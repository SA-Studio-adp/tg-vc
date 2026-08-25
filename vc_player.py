"""
Owns the userbot (Pyrogram) + PyTgCalls connection and does the actual
voice-chat streaming: join, play, real pause/resume, real seek, leave.

This is the piece that replaces "upload the file as a channel post" —
instead the file is piped into the group's live voice chat via ffmpeg.
"""
import logging
from pytgcalls import PyTgCalls, filters as ptg_filters
from pytgcalls.types import MediaStream, Update
from pyrogram import Client

from config import API_ID, API_HASH, SESSION_STRING, CHAT_ID

log = logging.getLogger("vc_player")

pyro_client = Client(
    "userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True,
)

calls = PyTgCalls(pyro_client)

# Filled in by bot.py so we can trigger "play next" when a stream ends
# without vc_player needing to import bot.py (avoids circular imports).
on_stream_end_callback = None


@calls.on_update(ptg_filters.stream_end())
async def _stream_ended(_, update: Update):
    log.info("Stream ended in chat %s", update.chat_id)
    if on_stream_end_callback:
        await on_stream_end_callback()


async def start():
    """Call once at bot startup: logs in the userbot and starts PyTgCalls."""
    await pyro_client.start()
    await calls.start()
    log.info("Userbot connected, PyTgCalls ready")


async def stop():
    await calls.leave_call(CHAT_ID) if await _in_call() else None
    await pyro_client.stop()


async def _in_call() -> bool:
    try:
        active = await calls.calls()
        return CHAT_ID in active
    except Exception:
        return False


async def play(filepath: str, is_video: bool, start_seconds: float = 0.0):
    """(Re)joins the VC if needed and streams `filepath` from the start,
    or from `start_seconds` in when used for seeking."""
    ffmpeg_params = f"-ss {start_seconds}" if start_seconds else None
    stream = MediaStream(
        filepath,
        video_flags=MediaStream.Flags.IGNORE if not is_video else MediaStream.Flags.AUTO_DETECT,
        ffmpeg_parameters=ffmpeg_params,
    )
    await calls.play(CHAT_ID, stream)


async def pause():
    await calls.pause(CHAT_ID)


async def resume():
    await calls.resume(CHAT_ID)


async def leave():
    if await _in_call():
        await calls.leave_call(CHAT_ID)


async def current_position() -> float:
    """Seconds into the current stream — used as the base for seek math."""
    try:
        return await calls.time(CHAT_ID)
    except Exception:
        return 0.0
