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
currently playing. This is also what satisfies Render's "web service"
requirement of binding a port — without it, Render eventually times out
the deploy even though the bot itself is working fine over Telegram
long-polling.

From the dashboard you can:
- **Add** a link to the queue (video/audio/file) — no need to be in the
  Telegram chat
- **Remove** any queued item that isn't currently playing
- **Pause / Resume** the live stream
- **Skip** to the next item
- **End VC** — clears the queue and fully ends the voice chat

Set `DASHBOARD_TOKEN` in your env vars and open:
```
https://your-render-url.onrender.com/?token=your_token
```
Without a token set, the dashboard is unauthenticated — fine for local
testing, not recommended once deployed publicly.

## Fixing YouTube's "Sign in to confirm you're not a bot" / "The page needs to be reloaded" errors
Both of these come from the same underlying situation: YouTube periodically
changes its player JavaScript, and yt-dlp has to keep up. When yt-dlp falls
behind (or your requests come from a datacenter IP, like Render's), you'll
see one of:
- `Sign in to confirm you're not a bot`
- `The page needs to be reloaded.`
- Or, most deceptively: **the download "succeeds" but only audio comes
  through**, because video formats need the signature decryption that's
  breaking, while audio formats don't — so a loose format selector can
  silently fall back to audio-only instead of failing.

This bot now defends against all three:

1. **Player client fallback.** yt-dlp is told to try the `android` and `tv`
   player clients before `web` — these generally skip the JS signature
   solving that breaks most often. This alone fixes most "page needs to be
   reloaded" errors.
2. **Strict format selection.** The video format selector no longer has a
   silent "just give me anything" fallback. If a real video+audio
   combination can't be resolved, it fails with a clear error instead of
   quietly shipping an audio-only file.
3. **Post-download validation.** After downloading, `ffprobe` checks the
   actual file for a video stream. If a "video" request somehow still came
   back audio-only, it's rejected with a clear message rather than queued.
4. **ffprobe duration fallback.** If yt-dlp's own metadata is missing a
   duration (common during these same failures), the bot reads it straight
   from the downloaded file instead of showing 00:00.
5. **Cookies (see below).** Still recommended — most effective against the
   "sign in to confirm" bot-check specifically.

**This is an ongoing cat-and-mouse situation, not a one-time fix** — if
these errors come back later, it usually means YouTube shipped another
player change yt-dlp hasn't caught up with yet. The fix each time is:
```
pip install -U yt-dlp
```
then redeploy. Checking `requirements.txt`'s yt-dlp version against the
[latest release](https://github.com/yt-dlp/yt-dlp/releases) periodically
is worth doing if downloads start failing again.

## Fixing "Sign in to confirm you're not a bot" specifically (cookies)
YouTube increasingly blocks download requests coming from datacenter IPs
(which is what Render, most VPS providers, etc. use) unless there's proof
of a real logged-in session. The fix is to give yt-dlp cookies from your
own browser:

1. Log into YouTube normally in Chrome/Firefox on your computer.
2. Install a cookie-export extension, e.g. **"Get cookies.txt LOCALLY"**
   (Chrome Web Store) or an equivalent for Firefox.
3. While on youtube.com, use the extension to export cookies in
   **Netscape format** as `cookies.txt`.
4. Get that file onto your server:
   - **Render**: use **Secret Files** (Dashboard → your service →
     Environment → Secret Files). Upload `cookies.txt` there — Render
     mounts it at a path like `/etc/secrets/cookies.txt`.
   - **Local/VPS**: just place the file somewhere on disk, e.g.
     `./cookies.txt`.
5. Set `COOKIES_FILE` in your env vars to that path, e.g.
   `COOKIES_FILE=/etc/secrets/cookies.txt`.

Restart the bot — yt-dlp will now use those cookies for every YouTube
request. Treat `cookies.txt` like a password (don't commit it to your
repo) — it can log into that YouTube account.

Cookies do expire eventually (usually weeks to months); if the "sign in
to confirm" error comes back later, just re-export and re-upload.

**Note on Render Secret Files specifically:** they're mounted read-only,
but yt-dlp needs to write back to the cookies file it's given (to persist
refreshed session cookies). The bot handles this automatically — it
copies your `COOKIES_FILE` into the writable `DOWNLOAD_DIR` once at
startup and uses that copy, so you don't need to do anything extra beyond
setting `COOKIES_FILE` to the Secret File's mounted path.

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
