#!/usr/bin/env sh
# Runs the bgutil PO-token HTTP server in the background, waits for it
# to actually be listening, then execs the bot as the container's main
# process (PID 1) so it receives signals correctly (SIGTERM on Render
# redeploys/restarts) instead of a wrapper shell swallowing them.
set -e

echo "[start.sh] starting bgutil PO-token server on :4416 ..."
node /opt/bgutil-pot/server/build/main.js &
BGUTIL_PID=$!

# Give it a moment to bind its port; don't block startup forever if it
# fails, since the bot still runs (just without PO tokens) — downloader.py
# already reports that clearly via _friendly_blocked_message().
for i in $(seq 1 15); do
    if curl -fsS "http://127.0.0.1:4416/ping" >/dev/null 2>&1 || \
       curl -fsS -X POST "http://127.0.0.1:4416/get_pot" -H "Content-Type: application/json" -d '{}' >/dev/null 2>&1; then
        echo "[start.sh] PO-token server is up."
        break
    fi
    if ! kill -0 "$BGUTIL_PID" 2>/dev/null; then
        echo "[start.sh] WARNING: PO-token server process exited early — bot will run without PO token support." >&2
        break
    fi
    sleep 1
done

echo "[start.sh] starting bot..."
exec python bot.py
