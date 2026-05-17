#!/usr/bin/env bash
# Run gmail_sync, telegram_bot, and pipeline_worker in parallel inside a single Railway service.
# If any worker exits, terminate siblings and exit non-zero so Railway restarts the service.

set -uo pipefail

GMAIL_PID=""
TG_PID=""
PIPE_PID=""

cleanup() {
  trap - SIGTERM SIGINT EXIT
  echo "[start.sh] shutting down workers..." >&2
  [[ -n "$GMAIL_PID" ]] && kill -TERM "$GMAIL_PID" 2>/dev/null || true
  [[ -n "$TG_PID"    ]] && kill -TERM "$TG_PID"    2>/dev/null || true
  [[ -n "$PIPE_PID"  ]] && kill -TERM "$PIPE_PID"  2>/dev/null || true
  [[ -n "$GMAIL_PID" ]] && wait "$GMAIL_PID" 2>/dev/null || true
  [[ -n "$TG_PID"    ]] && wait "$TG_PID"    2>/dev/null || true
  [[ -n "$PIPE_PID"  ]] && wait "$PIPE_PID"  2>/dev/null || true
}
trap cleanup SIGTERM SIGINT

python -u gmail_sync.py &
GMAIL_PID=$!

python -u telegram_bot.py &
TG_PID=$!

python -u pipeline_worker.py &
PIPE_PID=$!

echo "[start.sh] gmail_sync pid=$GMAIL_PID  telegram_bot pid=$TG_PID  pipeline_worker pid=$PIPE_PID" >&2

# Wait for the first child to exit. wait -n returns that child's exit code.
wait -n
EXIT=$?
echo "[start.sh] a worker exited (code=$EXIT) — terminating siblings" >&2
cleanup
exit "$EXIT"
