"""
Downloads YouTube video / audio (and generic direct links) via yt-dlp,
producing a local file that ffmpeg pipes into Telegram over RTMP.

=== 2026 UPDATE: PO Token requirement ===
Through 2026, YouTube expanded its bot-detection so that most player
clients (not just 'web') now require a PO Token (Proof of Origin token)
alongside cookies. Without one, extraction fails with "Sign in to
confirm you're not a bot" or "The page needs to be reloaded" even with
valid, fresh cookies. This is the most common cause of that error today.

Fix strategy here:
  1. Wire in the `bgutil-ytdlp-pot-provider` plugin, which runs a small
     local token-generation service yt-dlp calls automatically. Optional
     but strongly recommended — see README setup steps below.
  2. Widen the player-client fallback list. Different clients have
     different PO token requirements; trying several in order means one
     client failing doesn't sink the whole download.
  3. Validate the downloaded file actually contains a video stream when
     one was requested (unchanged from before) — a broad format
     fallback can quietly "succeed" with audio only.
  4. Check the installed yt-dlp version on startup and warn loudly if
     it's stale, since this extractor code ages out within weeks.

=== Setup for PO token support (recommended) ===
  pip install -U bgutil-ytdlp-pot-provider
  # This plugin needs a small companion HTTP server (Node-based) to
  # actually generate tokens. Run it once, e.g. via Docker:
  #   docker run -d -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider
  # Then set POT_PROVIDER_BASE_URL=http://127.0.0.1:4416 in your env
  # (see config.py). If unset, yt-dlp just runs without PO tokens,
  # same as before — this is additive, not required to boot.

See also: https://github.com/yt-dlp/yt-dlp/issues/17405 (ongoing arms
race tracking issue) and https://github.com/Brainicism/bgutil-ytdlp-pot-provider
"""
import os
import re
import uuid
import shutil
import asyncio
import subprocess
import logging
from importlib.metadata import version as _pkg_version, PackageNotFoundError

import yt_dlp

from config import DOWNLOAD_DIR, COOKIES_FILE

log = logging.getLogger(__name__)

# Detect the PO token plugin WITHOUT importing it. It installs into
# yt-dlp's own plugin-discovery namespace at `yt_dlp_plugins/extractor/`,
# which yt-dlp scans and imports automatically on the first
# YoutubeDL() instantiation, using its own plugin loader — a mechanism
# that does NOT share Python's normal sys.modules cache the way a
# plain `import` does. An earlier version of this check called
# `importlib.import_module(...)` on the plugin module directly, which
# ran the plugin's module-level provider-registration code once via
# the normal import; yt-dlp's own loader then ran that same
# registration code AGAIN on the first download, and its registry
# rejects a second registration of the same provider key — producing
# "AssertionError: PoTokenProvider BgUtilHTTP already registered" on
# every single download. Fixed by checking installed *package*
# metadata instead, which touches no code at all — yt-dlp's own loader
# remains the only thing that ever imports the plugin module.
try:
    _pkg_version("bgutil-ytdlp-pot-provider")
    _POT_PROVIDER_AVAILABLE = True
except PackageNotFoundError:
    _POT_PROVIDER_AVAILABLE = False

POT_PROVIDER_BASE_URL = os.environ.get("POT_PROVIDER_BASE_URL", "").strip()

# Player clients to try, in order. Widened from the original android/web
# pair because different clients hit different PO token requirements —
# if one client's token requirement isn't satisfiable in your setup, the
# next one in line may not need a token at all, or needs a different kind.
#   - android: usually fine without a token for a while, but has been
#     increasingly gated through 2026; kept first since it's cheapest
#     when it works.
#   - ios: historically slow to get token-gated, good fallback.
#   - web_safari: separate token pool from plain 'web', sometimes works
#     when 'web' is rate-limited or blocked.
#   - web: kept last — most reliable WITH cookies + PO token, but the
#     most bot-detection-scrutinized client when those aren't present.
# 'tv' remains excluded: pairing it with cookies can invalidate the
# cookie session entirely rather than helping (known yt-dlp/YouTube
# interaction).
YT_PLAYER_CLIENTS = "android,ios,web_safari,web"

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

_MIN_YTDLP_VERSION = "2026.7.4"  # bump this periodically; see README


def _check_ytdlp_version():
    """Fails loudly and specifically instead of letting a stale yt-dlp
    produce a confusing 'sign in to confirm' error that looks like a
    cookie/token problem when it's really just an outdated extractor."""
    try:
        installed = _pkg_version("yt-dlp")
    except PackageNotFoundError:
        log.warning("Could not determine installed yt-dlp version.")
        return
    if installed < _MIN_YTDLP_VERSION:
        log.warning(
            "yt-dlp %s is older than the recommended minimum %s. YouTube's "
            "extractor breaks frequently — if downloads are failing, run "
            "`pip install -U yt-dlp` before investigating anything else.",
            installed, _MIN_YTDLP_VERSION,
        )


_check_ytdlp_version()

if not _POT_PROVIDER_AVAILABLE:
    log.info(
        "bgutil-ytdlp-pot-provider not installed — running without PO token "
        "support. YouTube may block downloads with 'Sign in to confirm "
        "you're not a bot' regardless of cookies. Install it and run the "
        "companion server for the most reliable downloads (see top of "
        "this file for setup)."
    )


class DownloadError(Exception):
    """Raised for download failures we want to explain in plain language
    rather than surfacing yt-dlp's raw traceback text."""


class DownloadResult:
    def __init__(self, filepath, title, duration, thumbnail_url=None):
        self.filepath = filepath
        self.title = title
        self.duration = duration  # seconds
        self.thumbnail_url = thumbnail_url


def _job_dir():
    d = os.path.join(DOWNLOAD_DIR, uuid.uuid4().hex[:10])
    os.makedirs(d, exist_ok=True)
    return d


_writable_cookies_path = None


def _get_writable_cookies_file():
    """yt-dlp opens the cookie file in a mode that can write updated
    session cookies back to it. Render's Secret Files (and similar
    read-only mounts) reject that with 'Read-only file system', even
    though reading works fine. Fix: copy it once to a writable location
    and use that copy from then on."""
    global _writable_cookies_path
    if not COOKIES_FILE or not os.path.isfile(COOKIES_FILE):
        return None
    if _writable_cookies_path and os.path.isfile(_writable_cookies_path):
        return _writable_cookies_path

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    dest = os.path.join(DOWNLOAD_DIR, "cookies.txt")
    shutil.copyfile(COOKIES_FILE, dest)
    _writable_cookies_path = dest
    return dest


def _base_opts() -> dict:
    """Options shared by every yt-dlp call."""
    extractor_args = {"youtube": {"player_client": [YT_PLAYER_CLIENTS]}}

    if POT_PROVIDER_BASE_URL:
        # Tells the bgutil plugin where its companion token-server lives.
        extractor_args["youtubepot-bgutilhttp"] = {
            "base_url": [POT_PROVIDER_BASE_URL]
        }

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractor_args": extractor_args,
        # A stale cached player-JS/signature timestamp is another common,
        # quiet cause of "page needs to be reloaded" — force a re-fetch
        # rather than trusting a cache that may predate YouTube's latest
        # player change.
        "cachedir": False,
    }
    cookies_path = _get_writable_cookies_file()
    if cookies_path:
        opts["cookiefile"] = cookies_path
    return opts


def _validate_url(url: str):
    if not url or not _URL_RE.match(url.strip()):
        raise DownloadError(
            "That doesn't look like a link (needs to start with http:// or "
            "https://). Paste the actual video/file URL, not a title or file ID."
        )


_IMAGE_CODECS = {"mjpeg", "png", "jpg", "gif", "bmp", "webp", "tiff"}


def _ffprobe_codecs(filepath: str) -> list:
    """Returns [(codec_type, codec_name), ...] for every stream in the
    file — used to tell a real video stream apart from a static image
    (jpg/png/mjpeg), which ffprobe also reports as codec_type=video but
    which the RTMP push would otherwise happily stream as a looping
    'video' rather than rejecting it. That's the mechanism behind
    generic CDN links occasionally streaming a square thumbnail on loop
    instead of the actual video — this catches that case."""
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,codec_name", "-of", "csv=p=0", filepath,
            ],
            stderr=subprocess.DEVNULL,
        )
        pairs = []
        for line in out.decode().splitlines():
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if len(parts) >= 2:
                # ffprobe's csv output orders fields alphabetically
                # (codec_name, codec_type) regardless of the order
                # requested in -show_entries — verified empirically.
                codec_name, codec_type = parts[0], parts[1]
                pairs.append((codec_type, codec_name))
        return pairs
    except Exception:
        return []


def _has_real_video_stream(filepath: str) -> bool:
    codecs = _ffprobe_codecs(filepath)
    return any(
        codec_type == "video" and codec_name not in _IMAGE_CODECS
        for codec_type, codec_name in codecs
    )


def _has_any_video_stream(filepath: str) -> bool:
    codecs = _ffprobe_codecs(filepath)
    return any(codec_type == "video" for codec_type, codec_name in codecs)


def _ffprobe_has_audio(filepath: str) -> bool:
    codecs = _ffprobe_codecs(filepath)
    return any(codec_type == "audio" for codec_type, codec_name in codecs)


def _ffprobe_duration(filepath: str) -> float:
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", filepath,
            ],
            stderr=subprocess.DEVNULL,
        )
        return float(out.strip())
    except Exception:
        return 0.0


def _resolve_duration(filepath: str, ytdlp_duration) -> float:
    """yt-dlp's reported duration is sometimes missing/zero when metadata
    extraction was incomplete (the same player-client issues that cause
    "page needs to be reloaded"). ffprobe reading the actual file is
    authoritative when yt-dlp's number looks wrong."""
    if ytdlp_duration and ytdlp_duration > 0:
        return ytdlp_duration
    return _ffprobe_duration(filepath)


def _friendly_blocked_message() -> str:
    tips = [
        "YouTube is blocking this download from this server (their side, "
        "not yours). This has gotten stricter through 2026 — cookies alone "
        "don't always clear it anymore; a 'proof of origin' (PO) token is "
        "often required too.",
    ]
    if not _POT_PROVIDER_AVAILABLE:
        tips.append(
            "PO token support isn't installed on this server — that's "
            "likely the actual cause. Run: pip install -U "
            "bgutil-ytdlp-pot-provider, start its companion server, and "
            "set POT_PROVIDER_BASE_URL. See the top of downloader.py for "
            "the exact steps."
        )
    elif not POT_PROVIDER_BASE_URL:
        tips.append(
            "bgutil-ytdlp-pot-provider is installed but POT_PROVIDER_BASE_URL "
            "isn't set, so it's not actually being used. Set it to your "
            "running companion server's URL (e.g. http://127.0.0.1:4416)."
        )
    else:
        tips.append(
            "PO token support is configured — if it's still failing, "
            "confirm the companion token server is actually running and "
            "reachable at POT_PROVIDER_BASE_URL, and that COOKIES_FILE "
            "points to fresh, currently-valid cookies (re-export them; "
            "expired cookies fail the same way as missing ones)."
        )
    tips.append("Also keep yt-dlp updated: pip install -U yt-dlp.")
    return " ".join(tips)


def _run_ytdlp(url: str, audio_only: bool) -> DownloadResult:
    _validate_url(url)
    job_dir = _job_dir()
    out_tmpl = os.path.join(job_dir, "%(id)s.%(ext)s")

    if audio_only:
        ydl_opts = {
            **_base_opts(),
            "format": "bestaudio/best",
            "outtmpl": out_tmpl,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "opus",   # cheap for the RTMP push to pipe as VC audio
                "preferredquality": "192",
            }],
        }
    else:
        # Deliberately does NOT fall back to a bare "best" at the end —
        # that's what let a broken video extraction silently downgrade to
        # an audio-only stream before. If no real video+audio combo
        # resolves, this fails loudly instead of shipping audio-only.
        ydl_opts = {
            **_base_opts(),
            "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080][vcodec!=none]",
            "outtmpl": out_tmpl,
            "merge_output_format": "mp4",
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            if audio_only:
                filepath = os.path.splitext(filepath)[0] + ".opus"
            elif info.get("requested_downloads"):
                # merged output filename can differ from prepare_filename
                # when yt-dlp merges separate video+audio into one file
                filepath = info["requested_downloads"][0].get("filepath", filepath)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "reloaded" in msg.lower() or "sign in" in msg.lower():
            raise DownloadError(_friendly_blocked_message())
        raise DownloadError(msg)

    if not audio_only:
        if not _has_any_video_stream(filepath):
            cleanup_job(filepath)
            raise DownloadError(
                "Only an audio stream came through for this video — YouTube "
                "didn't serve a usable video format this time (their side; "
                "often resolves on retry, or use /vplaym for audio instead)."
            )
        if not _has_real_video_stream(filepath):
            cleanup_job(filepath)
            raise DownloadError(
                "What came through was a static image, not real video "
                "footage — YouTube didn't serve the actual video stream "
                "this time. Try again, or use /vplaym for audio instead."
            )

    duration = _resolve_duration(filepath, info.get("duration"))
    title = info.get("title") or os.path.splitext(os.path.basename(filepath))[0]

    return DownloadResult(
        filepath=filepath,
        title=title,
        duration=duration,
        thumbnail_url=info.get("thumbnail"),
    )


async def download_youtube_video(url: str) -> DownloadResult:
    return await asyncio.to_thread(_run_ytdlp, url, False)


async def download_youtube_music(url: str) -> DownloadResult:
    return await asyncio.to_thread(_run_ytdlp, url, True)


def _run_direct_download(url: str) -> DownloadResult:
    _validate_url(url)
    job_dir = _job_dir()
    out_tmpl = os.path.join(job_dir, "file.%(ext)s")
    ydl_opts = {
        **_base_opts(),
        "outtmpl": out_tmpl,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
    except yt_dlp.utils.DownloadError as e:
        raise DownloadError(str(e))

    # Generic/CDN links (as opposed to sites yt-dlp has a dedicated
    # extractor for) sometimes serve a preview thumbnail instead of the
    # actual media — same codec_type=video as real footage, but a static
    # image. Reject that clearly instead of silently streaming a frozen
    # square thumbnail on loop.
    if not _has_any_video_stream(filepath) and not _ffprobe_has_audio(filepath):
        cleanup_job(filepath)
        raise DownloadError(
            "That link didn't resolve to a playable audio/video file — "
            "double check it's a direct link to the actual media, not a "
            "webpage or preview link."
        )
    if _has_any_video_stream(filepath) and not _has_real_video_stream(filepath):
        cleanup_job(filepath)
        raise DownloadError(
            "That link resolved to a static image, not a real video — "
            "this can happen with CDN/link-shortener URLs that serve a "
            "preview thumbnail instead of the actual file. Double check "
            "the link points straight at the video/audio file itself."
        )

    duration = _resolve_duration(filepath, info.get("duration"))
    # Generic extractors on CDN/link-forwarding sites often have no real
    # title metadata to offer and fall back to the bare ID from the URL
    # (e.g. "AgADrC122188"), which isn't very readable. Prefer that title
    # when it looks meaningful; otherwise fall back to the actual
    # downloaded filename (with its real extension) so it's at least
    # clear what kind of file it is.
    raw_title = info.get("title")
    fallback_title = os.path.basename(filepath)  # keeps real extension
    title = raw_title if raw_title and raw_title != info.get("id") else fallback_title

    return DownloadResult(
        filepath=filepath,
        title=title,
        duration=duration,
        thumbnail_url=info.get("thumbnail"),
    )


async def download_direct_file(url: str) -> DownloadResult:
    return await asyncio.to_thread(_run_direct_download, url)


def cleanup_job(filepath: str):
    """Remove the whole job directory a file belongs to."""
    job_dir = os.path.dirname(filepath)
    try:
        for f in os.listdir(job_dir):
            os.remove(os.path.join(job_dir, f))
        os.rmdir(job_dir)
    except Exception:
        pass
