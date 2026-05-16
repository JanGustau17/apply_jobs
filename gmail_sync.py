#!/usr/bin/env python3
"""
gmail_sync — Poll Gmail for recruiter mail, classify via GPT-4o, update applications.md.

Modes:
  python gmail_sync.py --auth      One-time OAuth setup, writes token.json
  python gmail_sync.py --once      Run a single sync pass and exit
  python gmail_sync.py             Loop forever, poll every POLL_INTERVAL_SECONDS

Hard rules:
  - Never add new rows to applications.md. Update existing rows only.
  - Match by normalized company name. If no match, log "unmatched".
  - Use canonical statuses from templates/states.yml.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from openai import OpenAI

ROOT = Path(__file__).resolve().parent
APPLICATIONS = ROOT / "data" / "applications.md"
LOG_FILE = ROOT / "data" / "gmail-sync-log.md"
STATE_FILE = ROOT / "data" / "gmail-sync-state.json"
STATES_FILE = ROOT / "templates" / "states.yml"
CREDENTIALS_FILE = ROOT / "credentials.json"
TOKEN_FILE = ROOT / "token.json"

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
POLL_INTERVAL_SECONDS = int(os.getenv("GMAIL_POLL_INTERVAL", "900"))

LABEL_TO_STATUS = {
    "rejection": "Rejected",
    "interview_invite": "Interview",
    "offer": "Offer",
}

CLASSIFY_PROMPT = """You classify a single email from a recruiter or company hiring system.
Return ONLY a single JSON object, no prose, with these fields:
- company: the company name, or null if you can't tell
- classification: one of "rejection" | "interview_invite" | "offer" | "followup_needed" | "other"
- interview_date: ISO 8601 if this is an interview invite with a date, else null
- summary_one_line: <=120 chars, neutral, factual

Subject: {subject}
From: {sender}
Body (truncated):
{body}
"""


@dataclass
class EmailMsg:
    msg_id: str
    sender: str
    subject: str
    body: str
    received: datetime


def load_canonical_statuses() -> set[str]:
    if not STATES_FILE.exists():
        return {"Evaluated", "Applied", "Responded", "Interview", "Offer", "Rejected", "Discarded", "SKIP"}
    data = yaml.safe_load(STATES_FILE.read_text())
    return set(data.get("states", {}).keys()) if isinstance(data.get("states"), dict) else set(data.get("states", []))


def normalize_company(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def auth_flow() -> Credentials:
    creds: Optional[Credentials] = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
        return creds
    if not CREDENTIALS_FILE.exists():
        sys.exit("credentials.json missing. Create a Google Cloud OAuth client (Desktop) and download it here.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    return creds


def load_token_from_env_if_present() -> None:
    blob = os.getenv("GMAIL_TOKEN_B64")
    if blob and not TOKEN_FILE.exists():
        TOKEN_FILE.write_bytes(base64.b64decode(blob))


def gmail_service():
    load_token_from_env_if_present()
    creds = auth_flow()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def read_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_history_id": None, "seen_ids": []}


def write_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def decode_body(payload: dict) -> str:
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        if part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
    return ""


def parse_message(svc, msg_id: str) -> EmailMsg:
    m = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
    subject = headers.get("subject", "")
    sender = headers.get("from", "")
    body = decode_body(m.get("payload", {}))[:4000]
    received = datetime.fromtimestamp(int(m["internalDate"]) / 1000, tz=timezone.utc)
    return EmailMsg(msg_id=msg_id, sender=sender, subject=subject, body=body, received=received)


def list_recent_recruiter_messages(svc, since_seconds: int = 86400 * 14) -> list[str]:
    """Heuristic: recent unread + likely recruiter signals in subject."""
    q = 'newer_than:14d (subject:(application OR interview OR offer OR position OR role OR opportunity OR thank OR regret) OR from:(careers OR recruiting OR talent OR no-reply OR notifications))'
    resp = svc.users().messages().list(userId="me", q=q, maxResults=50).execute()
    return [m["id"] for m in resp.get("messages", []) or []]


def classify(client: OpenAI, msg: EmailMsg) -> dict:
    prompt = CLASSIFY_PROMPT.format(subject=msg.subject, sender=msg.sender, body=msg.body)
    resp = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o"),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    return json.loads(resp.choices[0].message.content)


def update_application_status(company: str, new_status: str, summary: str) -> tuple[Optional[str], bool]:
    """Return (old_status, matched)."""
    text = APPLICATIONS.read_text()
    lines = text.splitlines(keepends=True)
    target = normalize_company(company)
    for i, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 7:
            continue
        if cols[1] in ("#", "---"):
            continue
        company_col = cols[3]
        if normalize_company(company_col) == target:
            old_status = cols[6]
            cols[6] = new_status
            # Append summary to notes column if present (last column before trailing empty)
            if len(cols) >= 10:
                cols[9] = (cols[9] + f" | gmail: {summary}").strip(" |")
            lines[i] = "| " + " | ".join(cols[1:-1]) + " |\n"
            APPLICATIONS.write_text("".join(lines))
            return old_status, True
    return None, False


def append_log(company: str, old_status: Optional[str], new_status: str, summary: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"- {ts} | {company} | {old_status or 'n/a'} → {new_status} | {summary}\n"
    if not LOG_FILE.exists():
        LOG_FILE.write_text("# Gmail Sync Log\n\n")
    with LOG_FILE.open("a") as fh:
        fh.write(line)


def sync_once() -> None:
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY missing in .env")
    client = OpenAI(api_key=api_key)
    canonical = load_canonical_statuses()
    svc = gmail_service()
    state = read_state()
    raw_seen = state.get("seen_ids", []) or []
    seen: set[str] = set()
    for entry in raw_seen:
        if isinstance(entry, str):
            seen.add(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("id"), str):
            seen.add(entry["id"])
    ids = [i for i in list_recent_recruiter_messages(svc) if isinstance(i, str)]
    new_ids = [i for i in ids if i not in seen]
    print(f"[gmail-sync] candidates={len(ids)} new={len(new_ids)}", flush=True)
    for msg_id in new_ids:
        try:
            msg = parse_message(svc, msg_id)
            result = classify(client, msg)
            company = result.get("company")
            label = result.get("classification")
            summary = result.get("summary_one_line", "")
            if not company or label not in LABEL_TO_STATUS and label != "followup_needed":
                append_log(company or "?", None, "skip", f"{label}: {summary}")
            elif label == "followup_needed":
                append_log(company, None, "followup-needed", summary)
            else:
                new_status = LABEL_TO_STATUS[label]
                if new_status not in canonical:
                    append_log(company, None, "non-canonical-status", f"{new_status} not in states.yml")
                else:
                    old, matched = update_application_status(company, new_status, summary)
                    if matched:
                        append_log(company, old, new_status, summary)
                    else:
                        append_log(company, None, "unmatched", summary)
        except Exception as e:
            append_log("?", None, "error", f"{type(e).__name__}: {e}")
        seen.add(msg_id)
    state["seen_ids"] = list(seen)[-500:]
    write_state(state)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auth", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    if args.auth:
        load_token_from_env_if_present()
        auth_flow()
        print("OK. token.json written.")
        return

    if args.once:
        sync_once()
        return

    print(f"[gmail-sync] loop start (interval={POLL_INTERVAL_SECONDS}s)", flush=True)
    while True:
        try:
            sync_once()
        except Exception as e:
            print(f"[gmail-sync] error: {e}", flush=True)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
