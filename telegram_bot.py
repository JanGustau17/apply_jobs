#!/usr/bin/env python3
"""
telegram_bot — Telegram interface to the career-ops pipeline.

Commands / behavior:
  Send a job URL          → queued for auto-pipeline (evaluate + tracker entry)
  /status                 → pending items waiting on user input (numbered)
  /queue                  → what's currently being processed
  /applied                → last 10 applications from data/applications.md
  Reply "#N <your answer>" → resume app N with user input

Design notes:
  - Bot never blocks. URLs queued to data/pipeline.md, processed by external worker.
  - "Pending input" items live in data/pending-input.json.
  - Drafts that need review (essays, cover letters) get pushed here for approval.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

ROOT = Path(__file__).resolve().parent
PIPELINE = ROOT / "data" / "pipeline.md"
APPLICATIONS = ROOT / "data" / "applications.md"
PENDING_FILE = ROOT / "data" / "pending-input.json"
QUEUE_FILE = ROOT / "data" / "queue.json"

URL_RE = re.compile(r"https?://\S+")
REPLY_RE = re.compile(r"^#(\d+)\s+(.+)$", re.DOTALL)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# Silence libs that log full request URLs (would leak bot token).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("telegram-bot")


def read_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def authorized(user_id: int) -> bool:
    allow = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
    if not allow:
        return True
    return str(user_id) in {x.strip() for x in allow.split(",") if x.strip()}


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "career-ops bot ready.\n\n"
        "Send a job URL to queue evaluation.\n"
        "/status — pending review items\n"
        "/queue  — what's processing\n"
        "/applied — last 10 applications\n"
        "Reply with `#N <answer>` to resume an item."
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    pending = read_json(PENDING_FILE, [])
    if not pending:
        await update.message.reply_text("No items pending your input.")
        return
    lines = ["Pending input:"]
    for item in pending:
        lines.append(f"#{item['id']} — {item['company']} — {item['role']}: {item['reason']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    queue = read_json(QUEUE_FILE, [])
    if not queue:
        await update.message.reply_text("Queue empty.")
        return
    lines = ["Processing:"]
    for item in queue[-10:]:
        lines.append(f"- {item.get('status', '?')} | {item.get('url', '?')}")
    await update.message.reply_text("\n".join(lines))


async def cmd_applied(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not APPLICATIONS.exists():
        await update.message.reply_text("applications.md missing.")
        return
    rows = [ln for ln in APPLICATIONS.read_text().splitlines() if ln.startswith("|") and not ln.startswith("| #") and not ln.startswith("|---")]
    if not rows:
        await update.message.reply_text("No applications yet.")
        return
    last = rows[-10:]
    await update.message.reply_text("Last 10:\n" + "\n".join(last))


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    text = (update.message.text or "").strip()

    reply_m = REPLY_RE.match(text)
    if reply_m:
        app_id, answer = reply_m.group(1), reply_m.group(2).strip()
        pending = read_json(PENDING_FILE, [])
        match = next((p for p in pending if str(p.get("id")) == app_id), None)
        if not match:
            await update.message.reply_text(f"No pending item #{app_id}.")
            return
        match["answer"] = answer
        match["resolved_at"] = datetime.utcnow().isoformat()
        resolved_dir = ROOT / "data" / "resolved-input"
        resolved_dir.mkdir(parents=True, exist_ok=True)
        (resolved_dir / f"{app_id}.json").write_text(json.dumps(match, indent=2))
        write_json(PENDING_FILE, [p for p in pending if str(p.get("id")) != app_id])
        await update.message.reply_text(f"#{app_id} resumed with your answer.")
        return

    urls = URL_RE.findall(text)
    if urls:
        PIPELINE.parent.mkdir(parents=True, exist_ok=True)
        if not PIPELINE.exists():
            PIPELINE.write_text("# Pipeline — pending URLs\n\n")
        with PIPELINE.open("a") as fh:
            for u in urls:
                fh.write(f"- [ ] {u}  <!-- queued via telegram {datetime.utcnow().isoformat()} -->\n")
        await update.message.reply_text(f"Queued {len(urls)} URL(s).")
        return

    await update.message.reply_text("Send a URL, /status, /queue, /applied, or reply `#N <answer>`.")


def main() -> None:
    load_dotenv(ROOT / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing in .env")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("applied", cmd_applied))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Telegram bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
