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

=== Client selection (corrected mid-2026) ===
PO tokens are platform-specific and NOT interchangeable (a Web token
can't be used on Android/iOS), and bgutil-ytdlp-pot-provider only
generates the web-family token. So YT_PLAYER_CLIENTS below is ordered
to spend its attempts on clients that token actually helps
(mweb, web_safari, web — yt-dlp's own PO Token Guide specifically
recommends mweb for this plugin) before falling back to android, which
our plugin can't help at all but which occasionally still works
token-free depending on the video.

=== Datacenter IPs (Render, most cloud hosts): the honest limit ===
Even a fully correct PO token setup frequently does NOT clear
YouTube's bot check on datacenter IP ranges — this is acknowledged by
bgutil-ytdlp-pot-provider's own maintainers (see their GitHub issue
#37), not a misconfiguration on this bot's end. If downloads still
fail with everything above set up correctly, set YT_PROXY to a
residential/non-datacenter proxy URL — that's the remaining reliable
fix as of mid-2026.

See also: https://github.com/yt-dlp/yt-dlp/issues/17405 (ongoing arms
race tracking issue), https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide,
and https://github.com/Brainicism/bgutil-ytdlp-pot-provider
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
import requests

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

# Optional residential/SOCKS5 proxy for YouTube requests specifically.
# See the big caveat below: PO tokens alone increasingly don't clear
# YouTube's bot check on datacenter IPs (Render's included) — a proxy
# through a non-datacenter exit is often the only thing that still
# reliably works as of mid-2026. Purely additive: unset means no proxy,
# exactly like before.
YT_PROXY = os.environ.get("YT_PROXY", "").strip()

# Player clients to try, in order. Corrected mid-2026 after checking
# yt-dlp's current PO Token Guide and bgutil-ytdlp-pot-provider's own
# docs — the previous list (android,ios,web_safari,web) was subtly
# wrong:
#   - PO tokens are platform-specific and NOT interchangeable — a Web
#     token cannot be used on Android or iOS at all (yt-dlp's PO Token
#     Guide is explicit about this). Android/iOS need their own
#     DroidGuard/iOSGuard-generated tokens, which bgutil-ytdlp-pot-
#     provider does NOT generate (it only does the web-family
#     BotGuard token). So the old list spent its first two attempts on
#     clients our installed plugin literally cannot help, before ever
#     reaching one it can.
#   - yt-dlp's own PO Token Guide TL;DR, current as of this fix:
#     "Use a PO Token Provider plugin to provide the mweb client with
#     a PO Token for GVS requests" — mweb (mobile web), not web, is
#     the officially recommended pairing with this plugin.
# New order: mweb and web_safari first (both confirmed working with
# bgutil in yt-dlp's own recent issue tracker — #15789, #15571), web
# next (also works, just the most bot-detection-scrutinized), android
# as a last-ditch attempt in case a specific video happens to still be
# ungated on it. 'tv' remains excluded: pairing it with cookies can
# invalidate the cookie session entirely rather than helping.
YT_PLAYER_CLIENTS = "mweb,web_safari,web,android"

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
    def __init__(self, filepath, title, duration, thumbnail_url=None,
                 is_video=True, cover_path=None):
        self.filepath = filepath
        self.title = title
        self.duration = duration  # seconds
        self.thumbnail_url = thumbnail_url
        # is_video=False means this is an audio-only file — the RTMP
        # layer needs a static image to pair with it (see cover_path).
        self.is_video = is_video
        # Local path to a still image to display while streaming an
        # audio-only file: embedded cover art for direct-link audio, or
        # the downloaded YouTube thumbnail for /vplaym. None means no
        # art was available — rtmp_streamer falls back to a black frame.
        self.cover_path = cover_path


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
    if YT_PROXY:
        # See YT_PROXY's definition above: on datacenter hosts, this is
        # often the only thing that actually clears YouTube's bot check,
        # even with PO tokens and cookies both correctly configured.
        opts["proxy"] = YT_PROXY
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


def _extract_embedded_cover(filepath: str) -> str | None:
    """Extracts embedded cover art (the same "video" stream ffprobe
    reports for an mp3/m4a/flac's ID3 APIC tag — see _IMAGE_CODECS)
    to a standalone jpg alongside the source file, for use as the
    static video frame when RTMP-streaming an audio-only file. Returns
    None if extraction fails or the file has no embedded art at all
    (rtmp_streamer falls back to a black frame in that case)."""
    job_dir = os.path.dirname(filepath)
    cover_path = os.path.join(job_dir, "cover.jpg")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", filepath, "-an", "-map", "0:v:0",
             "-frames:v", "1", cover_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=True, timeout=15,
        )
        if os.path.isfile(cover_path) and os.path.getsize(cover_path) > 0:
            return cover_path
    except Exception:
        pass
    return None


def _download_thumbnail(url: str, job_dir: str) -> str | None:
    """Downloads a thumbnail URL (yt-dlp's info['thumbnail'] for
    /vplaym) to a local jpg for the same purpose as
    _extract_embedded_cover — a static frame for audio-only RTMP
    streaming. Separate from the embedded-art path because YouTube
    audio (converted to opus) has no embedded picture stream of its
    own; the thumbnail is the only art available for it."""
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        cover_path = os.path.join(job_dir, "cover.jpg")
        with open(cover_path, "wb") as f:
            f.write(resp.content)
        return cover_path
    except Exception:
        return None


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
        # Being straight about this rather than implying "configured
        # correctly" always fixes it: bgutil's own maintainers now warn
        # that PO tokens frequently don't clear the bot check at all on
        # datacenter IPs (Render included) — see
        # github.com/Brainicism/bgutil-ytdlp-pot-provider/issues/37.
        # Confirm the setup is actually right, but if it is, the real
        # fix left is routing through a non-datacenter IP.
        tips.append(
            "PO token support is configured — confirm the companion token "
            "server is actually running and reachable at "
            "POT_PROVIDER_BASE_URL, and that COOKIES_FILE points to fresh, "
            "currently-valid cookies (re-export them; expired cookies fail "
            "the same way as missing ones). Check the server logs for a "
            "'YouTube diagnostic trace' entry just above this error — it "
            "shows exactly which client and step (cookies, PO token "
            "request, or the bot check itself) actually failed, instead "
            "of guessing. But also know this: bgutil's "
            "own maintainers now warn that PO tokens frequently don't "
            "clear YouTube's bot check at all on datacenter IPs like "
            "Render's, even when everything is configured correctly — this "
            "isn't a config bug on your end, YouTube specifically targets "
            "hosting-provider IP ranges harder than PO tokens can offset. "
            "If setup checks out and it's still failing, the remaining "
            "reliable fix is routing YouTube requests through a "
            "residential/non-datacenter proxy: set the YT_PROXY env var "
            "to a proxy URL (e.g. http://user:pass@host:port) and this "
            "bot will use it automatically for YouTube only."
        )
    tips.append("Also keep yt-dlp updated: pip install -U yt-dlp.")
    return " ".join(tips)


class _DiagLogger:
    """Captures yt-dlp's own debug/info/warning/error lines for
    _diagnose_youtube_failure below. Kept separate from the normal
    quiet=True path so successful downloads stay quiet — this only
    runs once, after a sign-in/reloaded failure, specifically to
    surface which player client and PO-token step actually failed."""
    def __init__(self):
        self.lines = []

    def debug(self, msg):
        self.lines.append(msg)

    def info(self, msg):
        self.lines.append(msg)

    def warning(self, msg):
        self.lines.append(msg)

    def error(self, msg):
        self.lines.append(msg)


def _diagnose_youtube_failure(url: str):
    """Re-runs extract_info (metadata only, no download) with verbose
    logging on, purely to capture yt-dlp's internal client/PO-token
    negotiation trace and dump it to Render's (or wherever) logs at
    ERROR level. The normal quiet=True path never shows this detail,
    so a bare "Sign in to confirm you're not a bot" gives no way to
    tell WHICH client failed or whether a PO token was even attempted
    — this makes that visible without guessing at a fix blind a
    second time. Best-effort: any failure here is swallowed, since
    diagnostics must never mask or replace the real error being raised
    to the caller."""
    try:
        diag = _DiagLogger()
        opts = {
            **_base_opts(),
            "quiet": False,
            "no_warnings": False,
            "verbose": True,
            "logger": diag,
            "skip_download": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=False)
        except Exception:
            pass  # we only care about what got logged along the way

        # Keep only the lines that actually matter for this diagnosis —
        # which client was tried, whether a PO token was requested and
        # for what, and any explicit error — not yt-dlp's full verbose
        # dump (format lists, etc.) which would just bury the signal.
        relevant = [
            l for l in diag.lines
            if any(kw in l for kw in (
                "player_client", "Requesting", "Downloading", "player API",
                "PO Token", "pot", "POT", "GetPOT", "ERROR", "Sign in",
                "sign in", "bgutil", "cookies",
            ))
        ]
        log.error(
            "YouTube diagnostic trace for %s (which client/step actually "
            "failed):\n%s",
            url, "\n".join(relevant[-40:]) or "(no relevant lines captured)",
        )
    except Exception:
        log.exception("Diagnostic capture itself failed (non-fatal, ignoring)")


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
            _diagnose_youtube_failure(url)
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

    cover_path = None
    if audio_only:
        # YouTube audio has no embedded picture stream of its own (it's
        # a fresh opus re-encode) — use the video's thumbnail instead so
        # /vplaym still shows real cover art rather than a black frame.
        cover_path = _download_thumbnail(info.get("thumbnail"), job_dir)

    return DownloadResult(
        filepath=filepath,
        title=title,
        duration=duration,
        thumbnail_url=info.get("thumbnail"),
        is_video=not audio_only,
        cover_path=cover_path,
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
    # image, with no audio either. Reject THAT clearly.
    #
    # Careful distinction from a genuinely playable audio file: ffprobe
    # reports an mp3/m4a/flac's embedded cover art (ID3 APIC tag) the
    # exact same way — codec_type=video, codec_name=mjpeg/png — even
    # though the file is perfectly playable audio. The old version of
    # this check treated "has an image-coded video stream" as an error
    # on its own, which misfired on exactly that case (cover art +
    # real audio) and rejected legitimate audio files. The fix: only
    # treat it as the CDN-preview-thumbnail failure case when there's
    # NO real audio either — i.e. truly nothing playable came through.
    has_real_video = _has_real_video_stream(filepath)
    has_audio = _ffprobe_has_audio(filepath)

    if not has_real_video and not has_audio:
        cleanup_job(filepath)
        raise DownloadError(
            "That link didn't resolve to a playable audio/video file — "
            "double check it's a direct link to the actual media, not a "
            "webpage or preview link (or, if it's an image URL, that's "
            "expected to fail — this bot streams audio/video, not stills)."
        )

    cover_path = None
    if not has_real_video:
        # Audio-only (has_audio is True here, or the check above would
        # have already raised) — grab embedded cover art if there is
        # any; rtmp_streamer falls back to a black frame if not.
        cover_path = _extract_embedded_cover(filepath)

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
        is_video=has_real_video,
        cover_path=cover_path,
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
