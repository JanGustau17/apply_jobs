# Mode: gmail-sync

## Purpose

Continuously poll the candidate's Gmail inbox for recruiter messages, classify each one with OpenAI GPT-4o, match the message to an existing row in `data/applications.md`, and update its status. Append a human-readable audit line to `data/gmail-sync-log.md`.

## When to use

- Started as a background worker via `python gmail_sync.py` (locally) or via the `worker-gmail` process on Railway.
- The agent does NOT invoke this mode interactively. The worker runs every 15 minutes on its own loop.
- Manual one-shot: `python gmail_sync.py --once` for a single pass (useful for testing).

## Data contract

| File | Direction |
|------|-----------|
| `.env` (`OPENAI_API_KEY`, `GMAIL_*`) | read |
| `token.json` (Gmail OAuth) | read / refresh-write |
| `data/applications.md` | read + UPDATE status of existing rows only |
| `data/gmail-sync-log.md` | append |
| `data/gmail-sync-state.json` | read/write (last processed Gmail history ID) |

**Hard rules:**
- NEVER add a new row to `applications.md`. Only update existing rows.
- Match by normalized company name (case + punctuation insensitive). If no row matches, log "unmatched" and skip.
- All status writes go through the canonical states in `templates/states.yml` (same vocab as `normalize-statuses.mjs`).

## Classification (GPT-4o)

For each unread email from a likely recruiter sender, send subject + body to GPT-4o with this output contract:

```json
{
  "company": "string|null",
  "classification": "rejection|interview_invite|offer|followup_needed|other",
  "interview_date": "ISO 8601|null",
  "summary_one_line": "string"
}
```

Map `classification` → canonical status:

| GPT label | applications.md status |
|-----------|------------------------|
| `rejection` | `Rejected` |
| `interview_invite` | `Interview` |
| `offer` | `Offer` |
| `followup_needed` | (status unchanged) — update Follow-up Date column / notes only |
| `other` | (status unchanged) — log + skip |

## Log format

Each line in `data/gmail-sync-log.md`:

```
- YYYY-MM-DD HH:MM | {company} | {old_status} → {new_status} | {summary}
```

## Auth setup (one-time)

1. Create a Google Cloud project, enable Gmail API.
2. Create OAuth client (Desktop). Download `credentials.json` to repo root (gitignored).
3. Run `python gmail_sync.py --auth` once locally — opens browser, writes `token.json`.
4. For Railway deploy, set `GMAIL_TOKEN_B64` to the base64 of `token.json`.

## Why OpenAI (not Claude)

User has a free OpenAI key. Cost-free for this volume. Claude API is used everywhere else in career-ops; this is the ONE explicit exception.
