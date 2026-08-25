# VC Stream Bot

Streams YouTube/audio/direct-link media directly into a Telegram group's
**voice chat**, controlled via bot commands.

## Why this needs two Telegram identities

Telegram's Bot API has no way to join or stream into a voice chat — that's
only possible through the full MTProto client API, which means a real user
account ("userbot") has to do the actual joining/streaming. Your bot still
handles all the commands; it just delegates the streaming part to the
userbot behind the scenes via [py-tgcalls](https://pypi.org/project/py-tgcalls/).

So you need:
1. A **bot** (via @BotFather) — handles `/vplay`, `/vqueue`, etc.
2. A **userbot session** — a real account that joins the VC and streams.

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```
`ffmpeg` must also be installed on the system (it's what actually
transcodes/pipes the media into the call).

### 2. Get API credentials
Go to https://my.telegram.org → **API Development Tools** → create an app.
You'll get an `API_ID` and `API_HASH`.

### 3. Generate the userbot session
Run this **locally, once**, using the phone number of the account you want
to act as the streamer:
```bash
python generate_session.py
```
It'll ask for your API_ID/API_HASH, then walk you through the normal
Telegram login (phone number + code, and 2FA password if you have one).
At the end it prints a `SESSION_STRING` — copy that.

**This account must already be a member of the group** you want to stream
into, since it's the one that physically joins the voice chat.

### 4. Fill in `.env`
Copy `.env.example` to `.env` and fill in `BOT_TOKEN`, `API_ID`, `API_HASH`,
`SESSION_STRING`, `CHAT_ID` (the group ID), and `ADMIN_IDS`.

### 5. Run
```bash
python bot.py
```

## Commands
- `/vplay <url>` — queue + stream a YouTube video
- `/vplaym <url>` — queue + stream YouTube Music / audio-only
- `/vfile <url>` — queue + stream a direct file link
- `/vqueue` — show the queue
- `/vpause` / `/vresume` — real pause/resume in the voice chat
- `/vskip` — skip to the next item
- `/vstop` — clear the queue and **end** the voice chat (not just leave it —
  `close=True` actually discards the call for everyone)

The bot's `/` command menu in Telegram clients is synced automatically on
every startup from the list in `bot.py` (`BOT_COMMANDS`) — no need to
manually edit anything via @BotFather when you add or rename a command.

Unlike the old channel-post version, pause/resume here are **real** —
py-tgcalls actually pauses the live stream rather than just relabeling a
sent message.

## Web dashboard
The bot also runs a small web page (on `$PORT`) showing what's queued and
currently playing, with Pause / Resume / Skip / End VC buttons. This is
also what satisfies Render's "web service" requirement of binding a port —
without it, Render eventually times out the deploy even though the bot
itself is working fine over Telegram long-polling.

Set `DASHBOARD_TOKEN` in your env vars and open:
```
https://your-render-url.onrender.com/?token=your_token
```
Without a token set, the dashboard is unauthenticated — fine for local
testing, not recommended once deployed publicly.

## Notes / limitations
- Single voice chat at a time (`CHAT_ID` in config) — this isn't a
  multi-chat/multi-worker setup like the old per-chat queue was.
- The voice chat must already be active, or the group must allow the
  userbot to auto-start one (depends on `auto_start` in the call config —
  currently left at its py-tgcalls default).
- Hosting: this needs `ffmpeg` on PATH and a host that allows persistent
  processes with outbound UDP/TCP for the MTProto voice connection. Some
  free-tier hosts restrict this — a small VPS is the more reliable choice
  for the userbot side.
