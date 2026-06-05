"""
KDI 리포트 자동 수집·요약·알림 시스템
- KDI 사이트에서 새 리포트 감지
- Claude API로 요약
- Discord webhook + 이메일로 발송
"""

import os
import json
import sqlite3
import hashlib
import smtplib
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import httpx
import anthropic
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── 환경변수 ────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
DISCORD_WEBHOOK    = os.environ["DISCORD_WEBHOOK_URL"]
EMAIL_FROM         = os.environ.get("EMAIL_FROM", "")
EMAIL_TO           = os.environ.get("EMAIL_TO", "")
EMAIL_PASSWORD     = os.environ.get("EMAIL_PASSWORD", "")   # Gmail 앱 비밀번호
SMTP_HOST          = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT          = int(os.environ.get("SMTP_PORT", "587"))

# ── KDI 수집 대상 URL ────────────────────────────────────────────────────────
KDI_SOURCES = [
    {
        "name": "연구보고서",
        "url": "https://www.kdi.re.kr/research/reportList",
        "selector": "ul.report-list li",
        "emoji": "📄",
    },
    {
        "name": "KDI Focus",
        "url": "https://www.kdi.re.kr/research/focusList",
        "selector": "ul.report-list li",
        "emoji": "🔍",
    },
    {
        "name": "경제동향",
        "url": "https://www.kdi.re.kr/research/monTrends",
        "selector": "ul.report-list li",
        "emoji": "📊",
    },
]

BASE_URL = "https://www.kdi.re.kr"
DB_PATH  = Path("seen_reports.db")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

# ── 카테고리 분류 (Claude가 판단) ────────────────────────────────────────────
CATEGORIES = ["거시경제", "노동·복지", "IT·기술정책", "부동산", "금융·통화", "무역·산업", "기타"]


# ════════════════════════════════════════════════════════════════════════════
# DB
# ════════════════════════════════════════════════════════════════════════════

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id        TEXT PRIMARY KEY,
            title     TEXT,
            url       TEXT,
            source    TEXT,
            seen_at   TEXT
        )
    """)
    conn.commit()
    return conn


def is_new(conn: sqlite3.Connection, report_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM reports WHERE id = ?", (report_id,))
    return cur.fetchone() is None


def mark_seen(conn: sqlite3.Connection, report: dict) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO reports VALUES (?,?,?,?,?)",
        (report["id"], report["title"], report["url"], report["source"], datetime.now().isoformat()),
    )
    conn.commit()


# ════════════════════════════════════════════════════════════════════════════
# 스크래핑
# ════════════════════════════════════════════════════════════════════════════

def scrape_source(source: dict) -> list[dict]:
    """KDI 목록 페이지에서 리포트 링크·제목을 수집"""
    reports = []
    try:
        resp = httpx.get(source["url"], headers=HEADERS, timeout=20, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"스크래핑 실패 {source['name']}: {e}")
        return reports

    soup = BeautifulSoup(resp.text, "html.parser")

    # KDI 페이지 공통 패턴: 제목과 링크가 <a> 태그 안에 있음
    anchors = soup.select("a[href*='reportView'], a[href*='focusView'], a[href*='monTrends']")
    # fallback: 모든 내부 링크 중 연구 관련
    if not anchors:
        anchors = soup.select("a[href*='/research/']")

    seen_hrefs = set()
    for a in anchors:
        href = a.get("href", "")
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        title = a.get_text(strip=True)
        if len(title) < 5:          # 너무 짧은 건 메뉴 링크
            continue

        full_url = BASE_URL + href if href.startswith("/") else href
        report_id = hashlib.md5(full_url.encode()).hexdigest()[:12]

        reports.append({
            "id":     report_id,
            "title":  title,
            "url":    full_url,
            "source": source["name"],
            "emoji":  source["emoji"],
        })

    log.info(f"{source['name']}: {len(reports)}건 발견")
    return reports


# ════════════════════════════════════════════════════════════════════════════
# AI 요약
# ════════════════════════════════════════════════════════════════════════════

def fetch_page_text(url: str) -> str:
    """리포트 상세 페이지에서 본문 텍스트 추출"""
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # KDI 페이지 본문 선택자 (요약·초록 영역)
        for sel in ["div.report-abstract", "div.view-content", "div.cont-area", "article"]:
            el = soup.select_one(sel)
            if el:
                return el.get_text(separator="\n", strip=True)[:4000]

        # fallback: p 태그 전부
        paras = [p.get_text(strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 30]
        return "\n".join(paras)[:4000]
    except Exception as e:
        log.warning(f"본문 가져오기 실패: {e}")
        return ""


def summarize(report: dict) -> dict:
    """Claude API로 요약 + 카테고리 분류"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    page_text = fetch_page_text(report["url"])
    content_hint = f"\n\n[본문 발췌]\n{page_text}" if page_text else ""

    prompt = f"""다음은 한국개발연구원(KDI)의 리포트입니다.

제목: {report['title']}
출처: {report['source']}
URL: {report['url']}{content_hint}

아래 JSON 형식으로만 응답해. 다른 텍스트 없이 JSON만.

{{
  "summary": "3문장 이내 핵심 요약 (한국어)",
  "keywords": ["키워드1", "키워드2", "키워드3"],
  "category": "{'/'.join(CATEGORIES)} 중 하나",
  "relevance_score": 1~10 (경제·IT 정책 관련성, 숫자만),
  "one_liner": "트윗 길이(140자 이내) 한 줄 요약"
}}"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # JSON 펜스 제거
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(raw)
    except Exception as e:
        log.warning(f"요약 실패 ({report['title'][:30]}): {e}")
        result = {
            "summary": "요약을 생성할 수 없습니다.",
            "keywords": [],
            "category": "기타",
            "relevance_score": 5,
            "one_liner": report["title"],
        }

    return {**report, **result}


# ════════════════════════════════════════════════════════════════════════════
# Discord
# ════════════════════════════════════════════════════════════════════════════

def send_discord(report: dict) -> None:
    score_bar = "🟩" * report["relevance_score"] + "⬜" * (10 - report["relevance_score"])
    keywords  = " ".join(f"`{k}`" for k in report.get("keywords", []))

    payload = {
        "username": "KDI 리포트 봇",
        "avatar_url": "https://www.kdi.re.kr/favicon.ico",
        "embeds": [
            {
                "title": f"{report['emoji']} {report['title']}",
                "url":   report["url"],
                "color": 0x1D5AA0,   # KDI 파란색
                "fields": [
                    {"name": "📌 한 줄 요약", "value": report["one_liner"],           "inline": False},
                    {"name": "📋 요약",        "value": report["summary"],             "inline": False},
                    {"name": "🏷️ 키워드",      "value": keywords or "—",               "inline": True},
                    {"name": "📂 분류",         "value": report.get("category", "기타"), "inline": True},
                    {"name": "🔥 관련성",       "value": f"{score_bar} {report['relevance_score']}/10", "inline": False},
                ],
                "footer": {"text": f"KDI {report['source']} · {datetime.now().strftime('%Y-%m-%d')}"},
            }
        ],
    }

    try:
        resp = httpx.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        resp.raise_for_status()
        log.info(f"Discord 발송 완료: {report['title'][:40]}")
    except Exception as e:
        log.error(f"Discord 발송 실패: {e}")


# ════════════════════════════════════════════════════════════════════════════
# 이메일 (주간 다이제스트)
# ════════════════════════════════════════════════════════════════════════════

def build_email_html(reports: list[dict]) -> str:
    rows = ""
    for r in reports:
        keywords = ", ".join(r.get("keywords", []))
        rows += f"""
        <tr>
          <td style="padding:16px;border-bottom:1px solid #eee">
            <div style="font-size:13px;color:#888;margin-bottom:4px">
              {r['emoji']} {r['source']} · {r.get('category','기타')}
              &nbsp;|&nbsp; 관련성 {r.get('relevance_score','-')}/10
            </div>
            <a href="{r['url']}" style="font-size:16px;font-weight:600;color:#1D5AA0;text-decoration:none">
              {r['title']}
            </a>
            <p style="margin:8px 0 4px;font-size:14px;color:#333">{r.get('one_liner','')}</p>
            <p style="margin:0;font-size:13px;color:#555">{r.get('summary','')}</p>
            <p style="margin:6px 0 0;font-size:12px;color:#999">🏷️ {keywords}</p>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f5f5f5;font-family:sans-serif">
<table width="100%" cellpadding="0" cellspacing="0">
  <tr><td align="center" style="padding:24px">
    <table width="620" style="background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.1)">
      <tr><td style="background:#1D5AA0;padding:20px 24px">
        <h1 style="margin:0;color:#fff;font-size:20px">📊 KDI 리포트 다이제스트</h1>
        <p style="margin:4px 0 0;color:#b3cde8;font-size:13px">{datetime.now().strftime('%Y년 %m월 %d일')} · {len(reports)}건</p>
      </td></tr>
      <tr><td><table width="100%">{rows}</table></td></tr>
      <tr><td style="padding:16px 24px;background:#f9f9f9;font-size:12px;color:#aaa;text-align:center">
        KDI 리포트 자동 요약 시스템 · Powered by Claude
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""


def send_email(reports: list[dict]) -> None:
    if not all([EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD]):
        log.info("이메일 환경변수 미설정 — 건너뜀")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[KDI] 새 리포트 {len(reports)}건 · {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO

    html = build_email_html(reports)
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO.split(","), msg.as_string())
        log.info(f"이메일 발송 완료 → {EMAIL_TO}")
    except Exception as e:
        log.error(f"이메일 발송 실패: {e}")


# ════════════════════════════════════════════════════════════════════════════
# 메인
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    log.info("=== KDI 리포트 에이전트 시작 ===")
    conn = init_db()

    new_reports: list[dict] = []

    for source in KDI_SOURCES:
        for report in scrape_source(source):
            if not is_new(conn, report["id"]):
                continue

            log.info(f"새 리포트 발견: {report['title'][:50]}")
            summarized = summarize(report)

            # 관련성 낮은 리포트는 Discord만 건너뜀 (이메일은 모두 포함)
            if summarized.get("relevance_score", 0) >= 6:
                send_discord(summarized)

            mark_seen(conn, report)
            new_reports.append(summarized)

    if new_reports:
        send_email(new_reports)
        log.info(f"=== 완료: 총 {len(new_reports)}건 처리 ===")
    else:
        log.info("=== 새 리포트 없음 ===")

    conn.close()


if __name__ == "__main__":
    main()
