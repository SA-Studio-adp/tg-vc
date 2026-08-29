"""
RTMP-push streaming engine for the group's video chat — the ONLY
streaming engine now (vc_player.py / pytgcalls has been removed
entirely). Telegram just receives this as an ordinary incoming
broadcast (same mechanism as the "Stream with other apps" / OBS
dialog), so there's no client-side WebRTC video-track negotiation for
a library to get wrong — that negotiation is what was silently
falling back to audio-only before.

=== Audio-only items (/vplaym, /vfile on an audio file) ===
The group call here is created as an RTMP-source *video* chat, and
Telegram's RTMP ingest expects a video track — there's no "radio mode"
toggle the way pytgcalls' Flags.IGNORE offered. downloader.py detects
whether a download is actually audio-only (via ffprobe) regardless of
which command was used, and supplies a still image to loop as the
video track: embedded cover art for direct-link audio files, or the
downloaded YouTube thumbnail for /vplaym. Falls back to a synthesized
black frame only when no art is available.

=== Soft pause/resume ===
RTMP push has no native pause. pause() SIGTERMs ffmpeg after recording
how many seconds had played (via a monotonic clock started when
playback began); resume() restarts ffmpeg on the same file with
`-ss <that many seconds>`. There's a re-encode warm-up gap of roughly
a second on resume — inherent to restarting ffmpeg's own startup, not
a bug worth chasing.

=== Two bugs fixed here that showed up in production ===
1. "int too big to convert" from CreateGroupCall: Telegram's random_id
   is a SIGNED 32-bit int. The original `int.from_bytes(os.urandom(4),
   "big")` produced an unsigned value up to 2**32-1, which overflows
   int32 on roughly half of all random draws. Fixed by adding
   `signed=True`.
2. "PoTokenProvider BgUtilHTTP already registered" from yt-dlp: this
   used to be detected in downloader.py by explicitly
   `importlib.import_module`-ing the plugin. yt-dlp ALSO autodiscovers
   and imports that same plugin itself on first YoutubeDL()
   instantiation, via its own plugin loader — which doesn't share
   Python's normal sys.modules cache the way a plain `import` does. So
   the plugin's module-level registration code ran twice, and the
   second run hit an assertion that it was already registered. Fixed
   in downloader.py by checking the package is *installed* via
   importlib.metadata instead of importing its module — detection no
   longer imports anything, so yt-dlp's own loader is the only thing
   that ever does.
"""
import asyncio
import logging
import os
import time
import signal

from pyrogram import Client
from pyrogram.raw.functions.phone import (
    CreateGroupCall,
    DiscardGroupCall,
    GetGroupCallStreamRtmpUrl,
)
from pyrogram.raw.types import InputGroupCall

from config import API_ID, API_HASH, SESSION_STRING, CHAT_ID

log = logging.getLogger("rtmp_streamer")

pyro_client = Client(
    "userbot_rtmp",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True,
)

_active_call: InputGroupCall | None = None
_ffmpeg_proc: asyncio.subprocess.Process | None = None
_current_filepath: str | None = None
_current_is_video: bool = True
_current_cover_path: str | None = None         # static image for audio-only items, if one was extracted/downloaded
_elapsed_before_current_segment: float = 0.0   # seconds already played, across any prior pause(s)
_segment_started_at: float | None = None       # time.monotonic() when the currently-running ffmpeg segment began
_suppress_finish_callback: bool = False

# Set by bot.py. Called ONLY on a natural finish (end of file) or an
# ffmpeg error — never on pause()/stop()/skip, which the caller already
# knows about and handles itself. This mirrors the old vc_player's
# on_stream_end_callback so bot.py's queue-advance logic didn't need to
# change shape, just which module it calls.
on_finished_callback = None


async def start():
    await pyro_client.start()
    log.info("RTMP userbot connected")


async def stop():
    """Full shutdown: ends the call and disconnects the userbot. Call
    once at process shutdown (matches old vc_player.stop())."""
    await end_call()
    await pyro_client.stop()


async def _get_rtmp_credentials(peer, revoke: bool = False):
    """Fetches (server_url, stream_key) — identical to what the "Stream
    with other apps" dialog shows. revoke=False reuses the existing key
    if one's already been issued for this chat (matches app behavior:
    opening the dialog again doesn't rotate the key)."""
    result = await pyro_client.invoke(
        GetGroupCallStreamRtmpUrl(peer=peer, revoke=revoke)
    )
    return result.url, result.key


async def _ensure_rtmp_call() -> InputGroupCall:
    """Returns an InputGroupCall for an RTMP-source video chat in
    CHAT_ID, creating one if none exists yet."""
    global _active_call
    if _active_call is not None:
        return _active_call

    peer = await pyro_client.resolve_peer(CHAT_ID)
    # signed=True: see module docstring, bug #1.
    random_id = int.from_bytes(os.urandom(4), "big", signed=True)
    updates = await pyro_client.invoke(
        CreateGroupCall(peer=peer, random_id=random_id, rtmp_stream=True)
    )
    call = None
    for u in updates.updates:
        if hasattr(u, "call"):
            call = u.call
            break
    if call is None:
        raise RuntimeError(
            "Telegram didn't return the new call's details — try again; "
            "if it keeps happening, this chat may already have a "
            "non-RTMP video chat active that needs to be ended first."
        )

    _active_call = InputGroupCall(id=call.id, access_hash=call.access_hash)
    log.info("Created RTMP group call %s in chat %s", call.id, CHAT_ID)
    return _active_call


def _build_ffmpeg_cmd(filepath: str, is_video: bool, rtmp_target: str,
                       resume_seconds: float, cover_path: str | None = None) -> list:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
    # -ss must sit immediately before the -i of the file it seeks —
    # ffmpeg scopes it to that one input, not every input that follows.
    # Placed globally at the front (the earlier version of this
    # function did that) it silently seeks the WRONG input whenever
    # there's a second one ahead of the real media (cover image /
    # black-frame source) — meaning /vresume would restart audio-only
    # items from 0:00 instead of the paused position. Built as its own
    # list here and inserted right before the actual media file's -i
    # in every branch, so it always seeks the right thing.
    seek = ["-ss", f"{resume_seconds:.2f}"] if resume_seconds > 0 else []

    if is_video:
        cmd += [*seek, "-re", "-i", filepath]
        video_map, audio_map = ["-map", "0:v:0"], ["-map", "0:a:0?"]
    elif cover_path:
        # Audio-only source with real cover art (embedded ID3 art, or
        # the downloaded YouTube thumbnail): loop the still image as
        # the video track instead of a black frame. -shortest trims
        # the (infinite) looped image down to the audio's actual
        # length.
        cmd += ["-loop", "1", "-framerate", "25", "-i", cover_path]
        cmd += [*seek, "-re", "-i", filepath]
        video_map, audio_map = ["-map", "0:v:0"], ["-map", "1:a:0?"]
        cmd += ["-shortest"]
    else:
        # No cover art available — fall back to a synthetic black frame.
        cmd += ["-f", "lavfi", "-i", "color=c=black:s=1280x720:r=25"]
        cmd += [*seek, "-re", "-i", filepath]
        video_map, audio_map = ["-map", "0:v:0"], ["-map", "1:a:0?"]
        cmd += ["-shortest"]

    cmd += [
        *video_map, *audio_map,
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", "2500k",
        "-g", "60", "-keyint_min", "60",  # ~2s keyframe interval at 30fps — Telegram's RTMP ingest expects steady keyframes
        "-pix_fmt", "yuv420p",  # still-image inputs default to yuvj420p, which some RTMP receivers reject
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-f", "flv", rtmp_target,
    ]
    return cmd


async def _stop_ffmpeg_only(suppress_callback: bool):
    """Internal: stops the running ffmpeg process without touching the
    RTMP call itself, optionally suppressing on_finished_callback (used
    for pause/skip/replace, where the caller already knows and handles
    it — only a genuinely natural finish or error should auto-advance
    the queue)."""
    global _ffmpeg_proc, _suppress_finish_callback
    if _ffmpeg_proc is not None and _ffmpeg_proc.returncode is None:
        if suppress_callback:
            _suppress_finish_callback = True
        _ffmpeg_proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(_ffmpeg_proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            _ffmpeg_proc.kill()
    _ffmpeg_proc = None


async def _watch_ffmpeg(proc: asyncio.subprocess.Process, filepath: str):
    """Waits for ffmpeg to exit and logs stderr on a non-signal exit so
    RTMP rejection reasons aren't silently lost. Fires
    on_finished_callback only when this wasn't a suppressed
    (pause/skip/replace) stop."""
    global _suppress_finish_callback
    _, stderr = await proc.communicate()
    suppressed = _suppress_finish_callback
    _suppress_finish_callback = False

    if proc.returncode not in (0, None, -signal.SIGTERM):
        log.error(
            "ffmpeg RTMP push for %s exited with code %s:\n%s",
            filepath, proc.returncode, stderr.decode(errors="replace")[-2000:],
        )
    else:
        log.info("ffmpeg RTMP push for %s ended (suppressed=%s)", filepath, suppressed)

    if not suppressed and proc is _ffmpeg_proc and on_finished_callback:
        await on_finished_callback()


async def play(filepath: str, is_video: bool, resume_seconds: float = 0.0, cover_path: str | None = None):
    """Starts (or restarts) the RTMP push for `filepath`. Named `play`
    to match the old vc_player.play() call shape in bot.py.
    cover_path: for audio-only items (is_video=False), a still image to
    loop as the video track — embedded cover art or a downloaded
    thumbnail (see downloader.py). None falls back to a black frame."""
    global _ffmpeg_proc, _current_filepath, _current_is_video, _current_cover_path
    global _elapsed_before_current_segment, _segment_started_at

    await _stop_ffmpeg_only(suppress_callback=True)  # clean slate, no spurious advance

    peer = await pyro_client.resolve_peer(CHAT_ID)
    await _ensure_rtmp_call()
    server_url, stream_key = await _get_rtmp_credentials(peer)
    rtmp_target = server_url.rstrip("/") + "/" + stream_key

    cmd = _build_ffmpeg_cmd(filepath, is_video, rtmp_target, resume_seconds, cover_path)
    _ffmpeg_proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _current_filepath = filepath
    _current_is_video = is_video
    _current_cover_path = cover_path
    _elapsed_before_current_segment = resume_seconds
    _segment_started_at = time.monotonic()

    log.info("ffmpeg RTMP push started for %s (pid %s, resume=%.1fs, cover=%s)",
              filepath, _ffmpeg_proc.pid, resume_seconds, cover_path)
    asyncio.create_task(_watch_ffmpeg(_ffmpeg_proc, filepath))


async def pause():
    """Soft-pauses: stops ffmpeg, remembers elapsed playback time so
    resume() can restart from (approximately) the same spot."""
    global _elapsed_before_current_segment
    if _segment_started_at is not None:
        _elapsed_before_current_segment += time.monotonic() - _segment_started_at
    await _stop_ffmpeg_only(suppress_callback=True)


async def resume():
    """Resumes the currently-paused item from where pause() left off."""
    if _current_filepath is None:
        raise RuntimeError("Nothing to resume — no item is loaded.")
    await play(_current_filepath, _current_is_video,
               resume_seconds=_elapsed_before_current_segment,
               cover_path=_current_cover_path)


async def stop_playback():
    """Stops the current item without ending the call (used before
    skipping to the next queue item, or as part of end_call()).
    Suppressed so it doesn't ALSO fire on_finished_callback — callers
    that skip already advance the queue themselves."""
    global _elapsed_before_current_segment, _segment_started_at, _current_filepath, _current_cover_path
    await _stop_ffmpeg_only(suppress_callback=True)
    _elapsed_before_current_segment = 0.0
    _segment_started_at = None
    _current_filepath = None
    _current_cover_path = None


async def end_call():
    """Fully discards the RTMP group call, ending the video chat for
    everyone."""
    global _active_call
    await stop_playback()
    if _active_call is not None:
        try:
            await pyro_client.invoke(DiscardGroupCall(call=_active_call))
        except Exception:
            log.exception("Failed to discard RTMP group call cleanly")
        _active_call = None


def is_streaming() -> bool:
    return _ffmpeg_proc is not None and _ffmpeg_proc.returncode is None
