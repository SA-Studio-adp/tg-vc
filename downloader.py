"""
Downloads YouTube video / audio (and generic direct links) via yt-dlp,
producing a local file that pytgcalls' ffmpeg pipeline can stream from.

YouTube periodically changes its player JavaScript, which breaks yt-dlp's
signature decryption until yt-dlp catches up (this is an ongoing, known
arms race — see e.g. https://github.com/yt-dlp/yt-dlp/issues/17405).
Two symptoms of this:
  - "The page needs to be reloaded." errors
  - Silently downloading an audio-only stream when video was requested,
    because video formats need signature decryption and audio ones don't,
    so a broad format fallback can quietly "succeed" with audio only.

Mitigations here: try multiple YouTube player clients in order (some
don't need JS signature solving at all), validate the downloaded file
actually contains a video stream when one was requested, and use ffprobe
as an authoritative fallback for duration when yt-dlp's own metadata is
incomplete.
"""
import os
import re
import uuid
import shutil
import asyncio
import subprocess
import yt_dlp

from config import DOWNLOAD_DIR, COOKIES_FILE

# Player clients to try, in order. 'tv' is deliberately excluded: it
# authenticates differently from a normal browser session, and pairing
# it with cookies can invalidate the cookie session entirely rather than
# helping — a known yt-dlp/YouTube interaction, not a hypothetical.
# 'android' generally avoids the JS signature-solving issues that cause
# "page needs to be reloaded"; 'web' is kept as the client cookies work
# best with, for the "sign in to confirm" bot-check specifically.
YT_PLAYER_CLIENTS = "android,web"

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


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
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractor_args": {"youtube": {"player_client": [YT_PLAYER_CLIENTS]}},
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
    which py-tgcalls will otherwise happily stream as a looping 1-frame
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
                "preferredcodec": "opus",   # cheap for pytgcalls to pipe as VC audio
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
            raise DownloadError(
                "YouTube is blocking this download from this server (their "
                "side, not yours). This has gotten stricter through 2026 — "
                "cookies alone don't always clear it anymore; a 'proof of "
                "origin' token is often required now too. Make sure "
                "COOKIES_FILE is set with fresh cookies, keep yt-dlp updated "
                "(pip install -U yt-dlp), and see the README's YouTube "
                "section for current details — this is an active arms race "
                "that changes over time."
            )
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
