import asyncio
import logging
import threading

from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes

from config import BOT_TOKEN, ADMIN_IDS, CHAT_ID
from downloader import (
    download_youtube_video, download_youtube_music, download_direct_file,
    cleanup_job,
)
from queue_manager import queue, QueueItem, MediaType
import vc_player
import rtmp_streamer
import web_dashboard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vcbot")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


async def guard(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("🚫 Only bot admins can use this command.")
        return False
    return True


# ---------- playback control ----------

async def _play_item(item: QueueItem):
    """Actually starts streaming `item` into the voice chat."""
    is_video = item.media_type == MediaType.VIDEO
    await vc_player.play(item.filepath, is_video=is_video)
    queue.is_playing = True


async def _advance_and_play(bot):
    """Cleans up the item that just finished and starts the next one, if any."""
    old = queue.current
    nxt = queue.advance()
    if old:
        cleanup_job(old.filepath)
    if nxt:
        await _play_item(nxt)
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"▶️ Now streaming: <b>{nxt.title}</b> [{fmt_duration(nxt.duration)}]",
                parse_mode="HTML",
            )
        except Exception:
            pass
    else:
        await vc_player.end_call()
        try:
            await bot.send_message(chat_id=CHAT_ID, text="⏹ Queue finished — voice chat ended.")
        except Exception:
            pass
    return nxt


# ---------- commands ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>VC Stream Bot</b>\n\n"
        "<b>/vplay &lt;url&gt;</b> — queue + stream a YouTube video into the VC\n"
        "<b>/vplaym &lt;url&gt;</b> — queue + stream YouTube Music / audio\n"
        "<b>/vfile &lt;url&gt;</b> — queue + stream a direct file link\n"
        "<b>/vqueue</b> — show the current queue\n"
        "<b>/vpause</b> / <b>/vresume</b> — real pause/resume in the VC\n"
        "<b>/vskip</b> — skip to the next item\n"
        "<b>/vstop</b> — clear queue and end the VC\n\n"
        "<b>/rtplay &lt;url&gt;</b> — stream a YouTube video via RTMP push "
        "instead (alternative engine — try if /vplay's video won't show; "
        "no pause/resume/seek on this path, just play/stop)\n"
        "<b>/rtstop</b> — stop the RTMP stream\n\n"
        "There's also a web dashboard for controlling playback from a browser — "
        "ask whoever deployed the bot for the link.",
        parse_mode="HTML",
    )


DOWNLOADER_BY_TYPE = {
    MediaType.VIDEO: download_youtube_video,
    MediaType.AUDIO: download_youtube_music,
    MediaType.FILE: download_direct_file,
}


async def _enqueue_url(url: str, media_type: MediaType, requested_by: int = 0):
    """Bot-agnostic enqueue: downloads, adds to queue, and starts playback
    if the queue was empty. Used by both Telegram commands and the web
    dashboard's Add form. Posts status updates to CHAT_ID rather than
    replying to a specific message, since callers may not have one."""
    bot = _app_ref["app"].bot if _app_ref["app"] else None
    downloader_fn = DOWNLOADER_BY_TYPE[media_type]

    try:
        result = await downloader_fn(url)
    except Exception as e:
        if bot:
            await bot.send_message(chat_id=CHAT_ID, text=f"❌ Download failed: {e}")
        return

    item = QueueItem(
        title=result.title, url=url, media_type=media_type,
        requested_by=requested_by, filepath=result.filepath,
        duration=result.duration,
    )
    was_empty = queue.current is None
    queue.add(item)
    if was_empty:
        queue.current_index = 0
        try:
            await _play_item(item)
        except Exception as e:
            queue.clear()
            cleanup_job(item.filepath)
            if bot:
                await bot.send_message(chat_id=CHAT_ID, text=f"❌ Couldn't start the stream: {e}")
            return
        if bot:
            await bot.send_message(chat_id=CHAT_ID, text=f"▶️ Now streaming: {item.title} [{fmt_duration(item.duration)}]")
    else:
        if bot:
            await bot.send_message(chat_id=CHAT_ID, text=f"✅ Added to queue (position {len(queue.items)}): {item.title}")

    return item


async def _enqueue(update, context, url, media_type, downloader_fn):
    if not await guard(update):
        return
    if not url:
        await update.message.reply_text("Usage: send a link after the command, e.g.\n/vplay https://youtube.com/watch?v=...")
        return

    status_msg = await update.message.reply_text("⏳ Downloading…")
    try:
        result = await downloader_fn(url)
    except Exception as e:
        await status_msg.edit_text(f"❌ Download failed: {e}")
        return

    item = QueueItem(
        title=result.title, url=url, media_type=media_type,
        requested_by=update.effective_user.id, filepath=result.filepath,
        duration=result.duration,
    )
    was_empty = queue.current is None
    queue.add(item)
    if was_empty:
        queue.current_index = 0

    if was_empty:
        await status_msg.edit_text("🔊 Joining voice chat & starting stream…")
        try:
            await _play_item(item)
        except Exception as e:
            await status_msg.edit_text(
                f"❌ Couldn't start the stream: {e}\n\n"
                f"Make sure the userbot account is a member of the group and "
                f"a voice chat is active (or can be auto-started)."
            )
            queue.clear()
            cleanup_job(item.filepath)
            return
        await status_msg.edit_text(f"▶️ Now streaming: {item.title} [{fmt_duration(item.duration)}]")
    else:
        await status_msg.edit_text(f"✅ Added to queue (position {len(queue.items)}): {item.title}")


async def cmd_vplay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args) if context.args else None
    await _enqueue(update, context, url, MediaType.VIDEO, download_youtube_video)


async def cmd_vplaym(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args) if context.args else None
    await _enqueue(update, context, url, MediaType.AUDIO, download_youtube_music)


async def cmd_vfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args) if context.args else None
    await _enqueue(update, context, url, MediaType.FILE, download_direct_file)


async def cmd_vqueue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not queue.items:
        await update.message.reply_text("Queue is empty.")
        return
    lines = []
    for i, it in enumerate(queue.items):
        marker = "▶️" if i == queue.current_index else f"{i + 1}."
        lines.append(f"{marker} {it.title} [{fmt_duration(it.duration)}]")
    await update.message.reply_text("\n".join(lines))


async def cmd_vpause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not queue.current:
        await update.message.reply_text("Nothing is playing.")
        return
    await vc_player.pause()
    queue.is_playing = False
    await update.message.reply_text("⏸ Paused.")


async def cmd_vresume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not queue.current:
        await update.message.reply_text("Nothing is playing.")
        return
    await vc_player.resume()
    queue.is_playing = True
    await update.message.reply_text("▶️ Resumed.")


async def cmd_vskip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not queue.current:
        await update.message.reply_text("Nothing is playing.")
        return
    await update.message.reply_text("⏭ Skipping…")
    await _advance_and_play(context.bot)


async def cmd_vstop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    for it in queue.items:
        cleanup_job(it.filepath)
    queue.clear()
    await vc_player.end_call()
    await update.message.reply_text("🛑 Stopped and ended the voice chat.")


# ---------- RTMP streaming (alternative to /vplay) ----------
#
# Separate command family, not merged into the /vplay queue: RTMP push
# doesn't support real pause/resume/seek the way the pytgcalls path
# does (see rtmp_streamer.py's module docstring), so mixing the two
# queue models would be misleading. One RTMP item plays at a time;
# use /rtplay again to switch to a different video.

async def cmd_rtplay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    url = " ".join(context.args) if context.args else None
    if not url:
        await update.message.reply_text("Usage: /rtplay <youtube url>")
        return

    status_msg = await update.message.reply_text("⏳ Downloading…")
    try:
        result = await download_youtube_video(url)
    except Exception as e:
        await status_msg.edit_text(f"❌ Download failed: {e}")
        return

    await status_msg.edit_text("📡 Starting RTMP stream to the video chat…")
    try:
        await rtmp_streamer.start_stream(result.filepath)
    except Exception as e:
        await status_msg.edit_text(f"❌ Couldn't start the RTMP stream: {e}")
        cleanup_job(result.filepath)
        return
    await status_msg.edit_text(f"▶️ Streaming via RTMP: {result.title} [{fmt_duration(result.duration)}]")


async def cmd_rtstop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not rtmp_streamer.is_streaming():
        await update.message.reply_text("Nothing is RTMP-streaming right now.")
        return
    await rtmp_streamer.end_call()
    await update.message.reply_text("🛑 Stopped the RTMP stream and ended the video chat.")


# ---------- stream-end wiring ----------

async def _on_stream_end():
    """Called by vc_player when ffmpeg/py-tgcalls reports the current
    stream finished — auto-advances the queue."""
    app = _app_ref["app"]
    if app is None:
        return
    await _advance_and_play(app.bot)


_app_ref = {"app": None}


BOT_COMMANDS = [
    BotCommand("vplay", "Queue + stream a YouTube video into the VC"),
    BotCommand("vplaym", "Queue + stream YouTube Music / audio"),
    BotCommand("vfile", "Queue + stream a direct file link"),
    BotCommand("vqueue", "Show the current queue"),
    BotCommand("vpause", "Pause the stream"),
    BotCommand("vresume", "Resume the stream"),
    BotCommand("vskip", "Skip to the next item"),
    BotCommand("vstop", "Clear queue and end the voice chat"),
    BotCommand("rtplay", "Stream a YouTube video via RTMP push"),
    BotCommand("rtstop", "Stop the RTMP stream"),
    BotCommand("help", "Show this bot's commands"),
]


async def _post_init(app: Application):
    _app_ref["app"] = app
    vc_player.on_stream_end_callback = _on_stream_end
    await vc_player.start()
    log.info("VC player started, userbot connected")

    await rtmp_streamer.start_bot()
    log.info("RTMP streamer started, userbot connected")

    # "Auto command updation": pushes the command list to Telegram every
    # startup, so the / menu in clients always matches what's in this
    # file — no manual editing via @BotFather needed.
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        log.info("Command menu synced with Telegram")
    except Exception as e:
        log.warning(f"Could not sync command menu: {e}")

    # Dashboard runs in its own thread since Flask's dev server is
    # blocking; hand it the running event loop so its HTTP handlers can
    # safely schedule coroutines back onto it.
    loop = asyncio.get_running_loop()
    threading.Thread(
        target=web_dashboard.run,
        args=(loop, _advance_and_play, _enqueue_url, lambda: _app_ref["app"].bot if _app_ref["app"] else None),
        daemon=True,
    ).start()
    log.info("Dashboard starting on port %s", web_dashboard.PORT if hasattr(web_dashboard, "PORT") else "?")


async def _post_shutdown(app: Application):
    await vc_player.stop()
    await rtmp_streamer.stop_bot()


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("vplay", cmd_vplay))
    app.add_handler(CommandHandler("vplaym", cmd_vplaym))
    app.add_handler(CommandHandler("vfile", cmd_vfile))
    app.add_handler(CommandHandler("vqueue", cmd_vqueue))
    app.add_handler(CommandHandler("vpause", cmd_vpause))
    app.add_handler(CommandHandler("vresume", cmd_vresume))
    app.add_handler(CommandHandler("vskip", cmd_vskip))
    app.add_handler(CommandHandler("vstop", cmd_vstop))
    app.add_handler(CommandHandler("rtplay", cmd_rtplay))
    app.add_handler(CommandHandler("rtstop", cmd_rtstop))
    log.info("Bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()
