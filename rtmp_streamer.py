"""
Streams into the group's video chat via RTMP push (ffmpeg -> Telegram's
RTMP ingest), the same mechanism as the "Stream with other apps" dialog
in the Telegram app (OBS-style). This is a separate path from
vc_player.py, which joins the call as a WebRTC participant via
pytgcalls. RTMP push has a real advantage for this bot's use case:
Telegram treats it as an ordinary incoming broadcast, so there's no
client-side video/audio track negotiation for pytgcalls to get wrong —
if pytgcalls' video detection is ever flaky again, this path sidesteps
the whole class of problem.

Trade-offs vs vc_player.py's approach, worth knowing before choosing
between /vplay (pytgcalls) and /rtmpplay (this):
  - RTMP push has a few seconds of inherent latency (Telegram buffers
    and re-encodes it) — fine for playing a video, not for anything
    needing tight sync with the room.
  - Pause/resume/seek aren't controllable through the RTMP connection
    itself the way pytgcalls exposes them — this module stops/restarts
    ffmpeg rather than pausing a live stream, so "pause" here means
    "the stream drops and viewers see 'stream ended' briefly," not a
    true pause. queue_manager's existing pause/resume commands are
    wired to vc_player, not this module, for that reason.
  - Requires the group call to exist as an RTMP-source call
    specifically (created with rtmp_stream=True below) — a call
    already joined via pytgcalls is a normal WebRTC call and can't
    also accept an RTMP push at the same time.

How the credentials in your screenshot map to this code: the "Server
URL" + "Stream Key" shown in Telegram's UI are exactly what
phone.GetGroupCallStreamRtmpUrl returns below — this module fetches
them via the same MTProto call the app itself makes when you open that
dialog, rather than you copying them out by hand each time.
"""
import asyncio
import logging
import os
import signal

from pyrogram import Client
from pyrogram.raw.functions.phone import (
    CreateGroupCall,
    DiscardGroupCall,
    GetGroupCall,
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

# Tracks the currently-active RTMP-source call and ffmpeg push process,
# so a second /rtmpplay can stop the first cleanly instead of leaving
# an orphaned call or ffmpeg process behind.
_active_call: InputGroupCall | None = None
_ffmpeg_proc: asyncio.subprocess.Process | None = None
_on_finished_callback = None  # set by bot.py, mirrors vc_player's pattern


async def start_bot():
    """Call once at startup, alongside vc_player.start()."""
    await pyro_client.start()
    log.info("RTMP userbot connected")


async def stop_bot():
    await stop_stream()
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
    CHAT_ID, creating one if none exists yet. Safe to call repeatedly —
    reuses `_active_call` if we already created one this run."""
    global _active_call
    if _active_call is not None:
        return _active_call

    peer = await pyro_client.resolve_peer(CHAT_ID)
    updates = await pyro_client.invoke(
        CreateGroupCall(peer=peer, random_id=int.from_bytes(os.urandom(4), "big"), rtmp_stream=True)
    )
    # CreateGroupCall's Updates payload carries the new call's
    # id/access_hash inside an UpdateGroupCall — dig it out rather than
    # assuming a fixed position, since other update types can be mixed in.
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


async def start_stream(filepath: str, loop: bool = False):
    """Starts pushing `filepath` into the group's video chat over RTMP.
    Stops any stream already in progress first. Set loop=True to repeat
    the file indefinitely (ffmpeg -stream_loop -1) until stop_stream()
    is called — useful for a single background/holding video.
    """
    await stop_stream()  # clean slate; also frees the previous ffmpeg proc

    peer = await pyro_client.resolve_peer(CHAT_ID)
    await _ensure_rtmp_call()
    server_url, stream_key = await _get_rtmp_credentials(peer)
    rtmp_target = server_url.rstrip("/") + "/" + stream_key

    # -re paces input at its native frame rate — required for RTMP push
    # (without it ffmpeg dumps the whole file as fast as disk I/O
    # allows, which Telegram's ingest doesn't buffer for and will
    # reject/stutter on). Re-encode rather than -c copy: Telegram's
    # RTMP ingest is picky about GOP/keyframe interval and expects a
    # steady keyframe roughly every 2s, which most downloaded files
    # don't already have.
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
    if loop:
        cmd += ["-stream_loop", "-1"]
    cmd += [
        "-re", "-i", filepath,
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", "2500k",
        "-g", "60", "-keyint_min", "60",  # ~2s keyframe interval at 30fps
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-f", "flv", rtmp_target,
    ]

    global _ffmpeg_proc
    _ffmpeg_proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    log.info("ffmpeg RTMP push started for %s (pid %s)", filepath, _ffmpeg_proc.pid)
    asyncio.create_task(_watch_ffmpeg(_ffmpeg_proc, filepath))


async def _watch_ffmpeg(proc: asyncio.subprocess.Process, filepath: str):
    """Waits for ffmpeg to exit (end of file, or error) and logs stderr
    on non-zero exit so RTMP rejection reasons aren't silently lost."""
    _, stderr = await proc.communicate()
    if proc.returncode not in (0, None, -signal.SIGTERM):
        log.error(
            "ffmpeg RTMP push for %s exited with code %s:\n%s",
            filepath, proc.returncode, stderr.decode(errors="replace")[-2000:],
        )
    else:
        log.info("ffmpeg RTMP push for %s finished normally", filepath)
    if _on_finished_callback and proc is _ffmpeg_proc:
        await _on_finished_callback()


async def stop_stream():
    """Stops the current ffmpeg push, if any. Leaves the RTMP call
    itself intact (reused by the next start_stream) — use end_call()
    to actually discard the video chat."""
    global _ffmpeg_proc
    if _ffmpeg_proc is not None and _ffmpeg_proc.returncode is None:
        _ffmpeg_proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(_ffmpeg_proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            _ffmpeg_proc.kill()
    _ffmpeg_proc = None


async def end_call():
    """Fully discards the RTMP group call, ending the video chat for
    everyone — mirrors vc_player.end_call()'s close=True behavior."""
    global _active_call
    await stop_stream()
    if _active_call is not None:
        try:
            await pyro_client.invoke(DiscardGroupCall(call=_active_call))
        except Exception:
            log.exception("Failed to discard RTMP group call cleanly")
        _active_call = None


def is_streaming() -> bool:
    return _ffmpeg_proc is not None and _ffmpeg_proc.returncode is None


def set_on_finished_callback(cb):
    """Mirrors vc_player's on_stream_end_callback pattern so bot.py can
    trigger 'play next' the same way for both streaming paths."""
    global _on_finished_callback
    _on_finished_callback = cb
