"""
Minimal web dashboard + health-check endpoint.

Two jobs at once:
1. Render (and most PaaS "web service" tiers) expect the process to bind
   a port and answer HTTP requests, or the deploy eventually times out —
   even though the bot itself works fine via Telegram long-polling.
2. Gives you a simple page to see what's queued/playing and control it
   without needing to be in the Telegram chat.

Runs in a background thread (Flask's dev server is fine for this scale);
coroutines it needs to trigger (pause/skip/etc.) are handed off to the
bot's asyncio event loop via run_coroutine_threadsafe.
"""
import asyncio
import logging

from flask import Flask, request, redirect, url_for, abort

from config import DASHBOARD_TOKEN, PORT
from queue_manager import queue

log = logging.getLogger("dashboard")

app = Flask(__name__)

# Set by bot.py at startup so HTTP handlers (running in Flask's own
# thread) can safely schedule coroutines onto the bot's event loop.
_loop_ref = {"loop": None}
# Set by bot.py so the dashboard can call back into bot.py without a
# circular import at module load time.
_hooks = {"advance_and_play": None, "get_bot": None}


def _run_coro(coro):
    loop = _loop_ref["loop"]
    if loop is None:
        log.warning("Dashboard fired before bot loop was ready; ignoring action")
        return
    asyncio.run_coroutine_threadsafe(coro, loop)


def _check_token():
    if DASHBOARD_TOKEN and request.args.get("token") != DASHBOARD_TOKEN:
        abort(403)


def _fmt(seconds):
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _t():
    return f"?token={DASHBOARD_TOKEN}" if DASHBOARD_TOKEN else ""


PAGE = """
<!doctype html>
<html>
<head>
  <title>VC Stream Bot</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="15">
  <style>
    body {{ font-family: -apple-system, Segoe UI, sans-serif; background: #0f0f10; color: #eee; padding: 24px; max-width: 640px; margin: auto; }}
    h1 {{ font-size: 20px; }}
    .card {{ background: #1c1c1f; border-radius: 12px; padding: 16px; margin-bottom: 12px; }}
    .now {{ border: 1px solid #3b82f6; }}
    .title {{ font-weight: 600; }}
    .meta {{ color: #999; font-size: 13px; margin-top: 4px; }}
    .btns a {{ display: inline-block; margin-top: 10px; margin-right: 8px; padding: 8px 14px; background: #3b82f6; color: white; border-radius: 8px; text-decoration: none; font-size: 14px; }}
    .btns a.danger {{ background: #ef4444; }}
    .queue-item {{ padding: 8px 0; border-bottom: 1px solid #2a2a2e; font-size: 14px; }}
    .empty {{ color: #777; }}
  </style>
</head>
<body>
  <h1>🎧 VC Stream Bot</h1>
  {now_playing}
  <div class="card">
    <div class="title">Queue ({count})</div>
    {queue_list}
  </div>
</body>
</html>
"""


@app.route("/")
def dashboard():
    _check_token()
    item = queue.current
    t = _t()

    if item:
        status = "▶️ Playing" if queue.is_playing else "⏸ Paused"
        now_playing = f"""
        <div class="card now">
          <div class="title">{status}: {item.title}</div>
          <div class="meta">{_fmt(item.duration)} · {item.media_type.value}</div>
          <div class="btns">
            <a href="/control/pause{t}">Pause</a>
            <a href="/control/resume{t}">Resume</a>
            <a href="/control/skip{t}">Skip</a>
            <a href="/control/stop{t}" class="danger">End VC</a>
          </div>
        </div>
        """
    else:
        now_playing = '<div class="card empty">Nothing playing.</div>'

    if queue.items:
        rows = ""
        for i, it in enumerate(queue.items):
            marker = "▶️ " if i == queue.current_index else f"{i + 1}. "
            rows += f'<div class="queue-item">{marker}{it.title} [{_fmt(it.duration)}]</div>'
    else:
        rows = '<div class="empty">Empty.</div>'

    return PAGE.format(now_playing=now_playing, count=len(queue.items), queue_list=rows)


@app.route("/health")
def health():
    # Plain health-check for Render's port scan — no token required.
    return {"status": "ok"}


@app.route("/control/pause")
def control_pause():
    _check_token()
    import vc_player
    _run_coro(vc_player.pause())
    queue.is_playing = False
    return redirect(url_for("dashboard", token=request.args.get("token")))


@app.route("/control/resume")
def control_resume():
    _check_token()
    import vc_player
    _run_coro(vc_player.resume())
    queue.is_playing = True
    return redirect(url_for("dashboard", token=request.args.get("token")))


@app.route("/control/skip")
def control_skip():
    _check_token()
    advance = _hooks["advance_and_play"]
    get_bot = _hooks["get_bot"]
    if advance and get_bot and get_bot():
        _run_coro(advance(get_bot()))
    return redirect(url_for("dashboard", token=request.args.get("token")))


@app.route("/control/stop")
def control_stop():
    _check_token()
    import vc_player
    from downloader import cleanup_job
    for it in queue.items:
        cleanup_job(it.filepath)
    queue.clear()
    _run_coro(vc_player.end_call())
    return redirect(url_for("dashboard", token=request.args.get("token")))


def run(loop, advance_and_play_fn, get_bot_fn):
    _loop_ref["loop"] = loop
    _hooks["advance_and_play"] = advance_and_play_fn
    _hooks["get_bot"] = get_bot_fn
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
