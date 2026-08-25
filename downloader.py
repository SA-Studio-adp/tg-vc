"""
Downloads YouTube video / audio (and generic direct links) via yt-dlp,
producing a local file that pytgcalls' ffmpeg pipeline can stream from.
"""
import os
import uuid
import asyncio
import yt_dlp

from config import DOWNLOAD_DIR, COOKIES_FILE


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


def _base_opts() -> dict:
    """Options shared by every yt-dlp call, notably cookies — YouTube
    frequently demands "sign in to confirm you're not a bot" for requests
    coming from datacenter IPs (e.g. Render's), and cookies from a real
    logged-in session are the standard fix."""
    opts = {}
    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def _run_ytdlp(url: str, audio_only: bool) -> DownloadResult:
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
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }
    else:
        ydl_opts = {
            **_base_opts(),
            "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
            "outtmpl": out_tmpl,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filepath = ydl.prepare_filename(info)
        if audio_only:
            filepath = os.path.splitext(filepath)[0] + ".opus"

    return DownloadResult(
        filepath=filepath,
        title=info.get("title", "Unknown title"),
        duration=info.get("duration") or 0,
        thumbnail_url=info.get("thumbnail"),
    )


async def download_youtube_video(url: str) -> DownloadResult:
    return await asyncio.to_thread(_run_ytdlp, url, False)


async def download_youtube_music(url: str) -> DownloadResult:
    return await asyncio.to_thread(_run_ytdlp, url, True)


def _run_direct_download(url: str) -> DownloadResult:
    job_dir = _job_dir()
    out_tmpl = os.path.join(job_dir, "file.%(ext)s")
    ydl_opts = {
        **_base_opts(),
        "outtmpl": out_tmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filepath = ydl.prepare_filename(info)

    return DownloadResult(
        filepath=filepath,
        title=info.get("title") or os.path.basename(filepath),
        duration=info.get("duration") or 0,
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
