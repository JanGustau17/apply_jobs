#!/usr/bin/env python3
"""
pipeline_worker — Poll data/pipeline.md every PIPELINE_INTERVAL seconds.
For each `- [ ] <url>` entry: fetch HTML, extract job details via GPT-4o,
append row to data/applications.md, mark the pipeline line done, ping Telegram.

No Claude Code dependency.

Env:
  OPENAI_API_KEY                  (required)
  OPENAI_MODEL                    (default gpt-4o)
  PIPELINE_INTERVAL               seconds between scans (default 600)
  PIPELINE_BATCH_SIZE             max URLs per cycle  (default 5)
  TELEGRAM_BOT_TOKEN              (required for notifications)
  TELEGRAM_NOTIFY_CHAT_ID         chat to notify; falls back to first id in TELEGRAM_ALLOWED_USER_IDS
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent
PIPELINE = ROOT / "data" / "pipeline.md"
APPLICATIONS = ROOT / "data" / "applications.md"
LOG_FILE = ROOT / "data" / "pipeline-worker-log.md"

POLL_INTERVAL_SECONDS = int(os.getenv("PIPELINE_INTERVAL", "600"))
BATCH_SIZE = int(os.getenv("PIPELINE_BATCH_SIZE", "5"))
HTTP_TIMEOUT = 30
MAX_PAGE_CHARS = 12000

LINE_PENDING_RE = re.compile(r"^- \[ \]\s+(\S+)(.*)$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# Silence libs that log full request URLs (would leak bot token / API keys).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("pipeline-worker")


EXTRACT_PROMPT = """Extract job posting details from this page text.
Return ONLY a single JSON object with these fields:
- company: company name (string) or null
- role: job title (string) or null
- location: short location string (e.g. "Remote — US", "NYC", "Berlin") or null
- summary_one_line: <=120 chars factual summary, no marketing fluff
- score: number 1-5 estimated match quality for a CS undergrad targeting security + applied AI roles; 1=irrelevant, 5=excellent fit

Page text:
{page}
"""


def strip_html(html: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_PAGE_CHARS]


def fetch(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (career-ops pipeline-worker)"}
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True, headers=headers) as cli:
        r = cli.get(url)
        r.raise_for_status()
        return r.text


def coerce_str(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    if isinstance(v, (int, float, bool)):
        return str(v)
    return None


def coerce_score(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f < 0:
        return 0.0
    if f > 5:
        return 5.0
    return round(f, 1)


def extract_with_gpt(client: OpenAI, url: str, page_text: str) -> dict:
    prompt = EXTRACT_PROMPT.format(page=page_text)
    resp = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o"),
        messages=[
            {"role": "system", "content": f"Source URL: {url}"},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    raw = resp.choices[0].message.content
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError(f"gpt returned non-dict: {type(data).__name__}={data!r}")
    return data


def read_pipeline_pending() -> list[tuple[int, str]]:
    if not PIPELINE.exists():
        return []
    out: list[tuple[int, str]] = []
    for idx, line in enumerate(PIPELINE.read_text().splitlines()):
        m = LINE_PENDING_RE.match(line)
        if m:
            url = m.group(1).rstrip(".,)")
            out.append((idx, url))
    return out


def mark_line(line_idx: int, ok: bool, note: str) -> None:
    if not PIPELINE.exists():
        return
    original = PIPELINE.read_text()
    trailing_nl = original.endswith("\n")
    lines = original.splitlines()
    if not (0 <= line_idx < len(lines)):
        return
    marker = "[x]" if ok else "[!]"
    new_line = re.sub(r"^- \[ \]", f"- {marker}", lines[line_idx], count=1)
    new_line = re.sub(r"\s*<!--.*?-->\s*$", "", new_line).rstrip()
    if note:
        new_line += f"  <!-- {note} -->"
    lines[line_idx] = new_line
    out = "\n".join(lines)
    if trailing_nl:
        out += "\n"
    PIPELINE.write_text(out)


def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def existing_row(company: str, role: str) -> bool:
    if not APPLICATIONS.exists():
        return False
    tc, tr = normalize(company), normalize(role)
    for line in APPLICATIONS.read_text().splitlines():
        if not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 5 or cols[1] in ("#", "---") or cols[1].startswith("-"):
            continue
        if normalize(cols[3]) == tc and normalize(cols[4]) == tr:
            return True
    return False


def next_num() -> int:
    if not APPLICATIONS.exists():
        return 1
    max_n = 0
    for line in APPLICATIONS.read_text().splitlines():
        if not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 2:
            continue
        try:
            n = int(cols[1])
            if n > max_n:
                max_n = n
        except ValueError:
            continue
    return max_n + 1


def ensure_applications_header() -> None:
    if APPLICATIONS.exists():
        return
    APPLICATIONS.parent.mkdir(parents=True, exist_ok=True)
    APPLICATIONS.write_text(
        "# Applications Tracker\n\n"
        "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
    )


def append_application(company: str, role: str, score: float, summary: str, url: str) -> int:
    ensure_applications_header()
    num = next_num()
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    safe_summary = summary.replace("|", "/").strip()
    safe_url = url.replace("|", "%7C")
    row = (
        f"| {num} | {date} | {company} | {role} | {score:.1f}/5 | Evaluated | ❌ | n/a "
        f"| gpt-pipeline: {safe_summary} — {safe_url} |\n"
    )
    with APPLICATIONS.open("a") as fh:
        fh.write(row)
    return num


def _first_allowed_user() -> Optional[str]:
    raw = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
    for p in raw.split(","):
        p = p.strip()
        if p:
            return p
    return None


def telegram_notify(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_NOTIFY_CHAT_ID") or _first_allowed_user()
    if not token or not chat:
        log.warning("telegram notify skipped (token or chat missing)")
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"},
            timeout=10,
        )
    except Exception as e:
        log.warning("telegram notify failed: %s", e)


def append_log(url: str, status: str, detail: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"- {ts} | {status} | {url} | {detail}\n"
    if not LOG_FILE.exists():
        LOG_FILE.write_text("# Pipeline Worker Log\n\n")
    with LOG_FILE.open("a") as fh:
        fh.write(line)


def process_once(client: OpenAI) -> None:
    pending = read_pipeline_pending()
    log.info("pending=%d batch=%d", len(pending), BATCH_SIZE)
    for line_idx, url in pending[:BATCH_SIZE]:
        try:
            html = fetch(url)
            page_text = strip_html(html)
            if not page_text:
                mark_line(line_idx, ok=False, note="empty page")
                append_log(url, "skip", "empty page text after strip")
                telegram_notify(f"⚠ empty page — {url}")
                continue
            data = extract_with_gpt(client, url, page_text)
            company = coerce_str(data.get("company"))
            role = coerce_str(data.get("role"))
            summary = coerce_str(data.get("summary_one_line")) or ""
            score = coerce_score(data.get("score", 0))
            if not company or not role:
                mark_line(line_idx, ok=False, note="missing company/role")
                append_log(url, "skip", f"company={company!r} role={role!r}")
                telegram_notify(f"⚠ skipped — no company/role\n{url}")
                continue
            if existing_row(company, role):
                mark_line(line_idx, ok=True, note=f"dup {company}")
                append_log(url, "duplicate", f"{company} / {role}")
                telegram_notify(f"↺ duplicate — {company} / {role}\n{url}")
                continue
            num = append_application(company, role, score, summary, url)
            mark_line(line_idx, ok=True, note=f"#{num} {company}")
            append_log(url, "added", f"#{num} {company} / {role} score={score}")
            telegram_notify(
                f"✅ #{num} {company} — {role}\nScore {score:.1f}/5\n{summary}\n{url}"
            )
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            where = tb[-1] if tb else None
            loc = f"{where.filename}:{where.lineno} in {where.name}" if where else "unknown"
            log.error("crash url=%s at %s: %s", url, loc, e)
            mark_line(line_idx, ok=False, note=f"error: {type(e).__name__}")
            append_log(url, "error", f"{type(e).__name__}: {e} @ {loc}")
            telegram_notify(f"❌ pipeline error — {type(e).__name__}: {e}\n{url}")


def main() -> None:
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        # Do not crash sibling workers via start.sh wait -n cascade. Idle instead.
        log.error("OPENAI_API_KEY missing — pipeline-worker disabled, sleeping forever")
        while True:
            time.sleep(3600)
    client = OpenAI(api_key=api_key)
    log.info("pipeline-worker loop start (interval=%ds, batch=%d)", POLL_INTERVAL_SECONDS, BATCH_SIZE)
    while True:
        try:
            process_once(client)
        except Exception as e:
            log.error("loop error: %s", e)
            log.error(traceback.format_exc())
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
