#!/data/data/com.termux/files/usr/bin/sh
# Keeps the watcher running: restarts it if it ever exits, logs to watcher.log.
# Settings live in .env next to this file (never committed). Example .env:
#   NTFY_TOPIC=your-topic
#   HEALTHCHECK_URL=https://hc-ping.com/your-uuid
#   CUTOFF_DATE=            # empty = any date
cd "$(dirname "$0")" || exit 1
set -a
[ -f .env ] && . ./.env
STATE_FILE="${STATE_FILE:-$(pwd)/local-state.json}"
set +a
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock
while true; do
  python -u watch.py 2>&1 | tee -a watcher.log
  echo "watcher exited at $(date); restarting in 30s" | tee -a watcher.log
  sleep 30
done
