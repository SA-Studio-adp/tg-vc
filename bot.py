import os
import io
import logging
import requests
from telegram import (
    Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
from telegram.constants import ParseMode

from config import BOT_TOKEN, CHANNEL_ID, ADMIN_IDS, MAX_UPLOAD_MB, DOWNLOAD_DIR
from downloader import (
    download_youtube_video, download_youtube_music, download_direct_file,
    trim_clip, probe_duration, cleanup_job
)
from queue_manager import get_queue, QueueItem, MediaType

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ytbot")

MAX_BYTES = MAX_UPLOAD_MB * 1024 * 1024


# ---------- helpers ----------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def player_caption(item: QueueItem, queue) -> str:
    status = "▶️ Playing" if queue.is_playing else "⏸ Paused"
    pos = fmt_duration(item.elapsed)
    dur = fmt_duration(item.duration)
    upcoming = ""
    if queue.has_next():
        nxt = queue.items[queue.current_index + 1]
        upcoming = f"\n\n<b>⏭ Up next:</b> {nxt.title}"
    return (
        f"<b>{item.title}</b>\n"
        f"{status}   {pos} / {dur}\n"
        f"Queue position: {queue.current_index + 1}/{len(queue.items)}"
        f"{upcoming}"
    )


def player_keyboard(item: QueueItem, queue) -> InlineKeyboardMarkup:
    play_pause = "⏸ Pause" if queue.is_playing else "▶️ Play"
    rows = [
        [
            InlineKeyboardButton("⏪ -5s", callback_data=f"seek:-5:{item.id}"),
            InlineKeyboardButton(play_pause, callback_data=f"toggle:{item.id}"),
            InlineKeyboardButton("+5s ⏩", callback_data=f"seek:5:{item.id}"),
        ],
        [
            InlineKeyboardButton("⏭ Play Next", callback_data=f"next:{item.id}"),
            InlineKeyboardButton("🗑 Remove", callback_data=f"remove:{item.id}"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def download_thumb_bytes(thumb_url: str):
    if not thumb_url:
        return None
    try:
        r = requests.get(thumb_url, timeout=15)
        r.raise_for_status()
        return io.BytesIO(r.content)
    except Exception as e:
        log.warning(f"thumbnail fetch failed: {e}")
        return None


async def send_player_card(context: ContextTypes.DEFAULT_TYPE, chat_id, item: QueueItem, queue):
    """Uploads the media file to the channel with embedded thumbnail,
    title/status caption, and the control-panel buttons underneath."""
    thumb = download_thumb_bytes(item.thumbnail_url)
    caption = player_caption(item, queue)
    kb = player_keyboard(item, queue)

    with open(item.filepath, "rb") as f:
        if item.media_type == MediaType.AUDIO:
            msg = await context.bot.send_audio(
                chat_id=chat_id, audio=InputFile(f, filename=os.path.basename(item.filepath)),
                thumbnail=thumb, caption=caption, parse_mode=ParseMode.HTML,
                duration=int(item.duration), reply_markup=kb,
            )
        else:
            msg = await context.bot.send_video(
                chat_id=chat_id, video=InputFile(f, filename=os.path.basename(item.filepath)),
                thumbnail=thumb, caption=caption, parse_mode=ParseMode.HTML,
                duration=int(item.duration), supports_streaming=True, reply_markup=kb,
            )
    item.message_id = msg.message_id
    return msg


async def refresh_player_card(context: ContextTypes.DEFAULT_TYPE, chat_id, item: QueueItem, queue):
    try:
        await context.bot.edit_message_caption(
            chat_id=chat_id, message_id=item.message_id,
            caption=player_caption(item, queue), parse_mode=ParseMode.HTML,
            reply_markup=player_keyboard(item, queue),
        )
    except Exception as e:
        log.warning(f"could not refresh card: {e}")


# ---------- command handlers ----------

async def guard(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("🚫 Only bot admins can use this command.")
        return False
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>YT Channel Player Bot</b>\n\n"
        "<b>/yt &lt;url&gt;</b> — queue a YouTube video\n"
        "<b>/ytm &lt;url&gt;</b> — queue YouTube Music / audio\n"
        "<b>/files &lt;url&gt;</b> — queue a direct audio/video file link\n"
        "<b>/queue</b> — show the current queue\n"
        "<b>/skip</b> — skip to the next item\n"
        "<b>/clear</b> — clear the queue\n",
        parse_mode=ParseMode.HTML,
    )


async def _enqueue_and_maybe_play(update, context, url, media_type, downloader_fn):
    if not await guard(update):
        return
    if not url:
        await update.message.reply_text("Usage: send a link after the command, e.g.\n/yt https://youtube.com/watch?v=...")
        return

    status_msg = await update.message.reply_text("⏳ Downloading…")
    try:
        result = await downloader_fn(url)
    except Exception as e:
        await status_msg.edit_text(f"❌ Download failed: {e}")
        return

    size = os.path.getsize(result.filepath)
    if size > MAX_BYTES:
        await status_msg.edit_text(
            f"❌ File is {size / 1024 / 1024:.1f}MB, over the {MAX_UPLOAD_MB}MB bot upload limit.\n"
            f"Run a local Bot API server for up to 2GB uploads (see README)."
        )
        cleanup_job(result.filepath)
        return

    queue = get_queue(update.effective_chat.id)
    item = QueueItem(
        title=result.title, url=url, media_type=media_type,
        requested_by=update.effective_user.id, filepath=result.filepath,
        thumbnail_url=result.thumbnail_url, duration=result.duration or probe_duration(result.filepath),
    )
    was_empty = queue.current is None
    queue.add(item)

    if was_empty:
        await status_msg.edit_text("📤 Uploading & starting playback…")
        await send_player_card(context, CHANNEL_ID, item, queue)
        await status_msg.delete()
    else:
        await status_msg.edit_text(f"✅ Added to queue (position {len(queue.items)}): {item.title}")


async def cmd_yt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args) if context.args else None
    await _enqueue_and_maybe_play(update, context, url, MediaType.VIDEO, download_youtube_video)


async def cmd_ytm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args) if context.args else None
    await _enqueue_and_maybe_play(update, context, url, MediaType.AUDIO, download_youtube_music)


async def cmd_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args) if context.args else None
    await _enqueue_and_maybe_play(update, context, url, MediaType.FILE, download_direct_file)


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    queue = get_queue(update.effective_chat.id)
    if not queue.items:
        await update.message.reply_text("Queue is empty.")
        return
    lines = []
    for i, it in enumerate(queue.items):
        marker = "▶️" if i == queue.current_index else f"{i + 1}."
        lines.append(f"{marker} {it.title} [{fmt_duration(it.duration)}]")
    await update.message.reply_text("\n".join(lines))


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await _advance(context, update.effective_chat.id)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    get_queue(update.effective_chat.id).clear()
    await update.message.reply_text("🗑 Queue cleared.")


# ---------- button callbacks ----------

async def _advance(context, chat_id):
    queue = get_queue(chat_id)
    old = queue.current
    nxt = queue.next()
    if old:
        cleanup_job(old.filepath)
    if nxt:
        await send_player_card(context, CHANNEL_ID, nxt, queue)
    return nxt


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not is_admin(user.id):
        await query.answer("🚫 Admins only.", show_alert=True)
        return

    action, *rest = query.data.split(":")
    chat_id = CHANNEL_ID
    queue = get_queue(chat_id)
    item = queue.current

    if action == "toggle":
        queue.is_playing = not queue.is_playing
        await query.answer("Paused" if not queue.is_playing else "Playing")
        if item:
            await refresh_player_card(context, chat_id, item, queue)

    elif action == "next":
        await query.answer("Skipping…")
        await _advance(context, chat_id)

    elif action == "remove":
        item_id = int(rest[1]) if len(rest) > 1 else int(rest[0])
        queue.remove(item_id)
        await query.answer("Removed from queue")

    elif action == "seek":
        delta = int(rest[0])
        if not item:
            await query.answer("Nothing playing")
            return
        new_pos = max(0, item.elapsed + delta)
        item.elapsed = new_pos
        await query.answer(f"Seeking to {fmt_duration(new_pos)}…")
        try:
            clip_path = trim_clip(item.filepath, new_pos, "mp4")
            with open(clip_path, "rb") as f:
                if item.media_type == MediaType.AUDIO:
                    await context.bot.send_audio(chat_id=chat_id, audio=InputFile(f), caption=f"⏩ Resumed at {fmt_duration(new_pos)}")
                else:
                    await context.bot.send_video(chat_id=chat_id, video=InputFile(f), caption=f"⏩ Resumed at {fmt_duration(new_pos)}", supports_streaming=True)
            os.remove(clip_path)
        except Exception as e:
            log.warning(f"seek failed: {e}")
            await context.bot.send_message(chat_id=chat_id, text="⚠️ Seek failed — ffmpeg couldn't trim this file.")
        await refresh_player_card(context, chat_id, item, queue)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("yt", cmd_yt))
    app.add_handler(CommandHandler("ytm", cmd_ytm))
    app.add_handler(CommandHandler("files", cmd_files))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()
