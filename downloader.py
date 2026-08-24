"""
Handles downloading YouTube video / YouTube Music audio via yt-dlp,
and probing/trimming generic direct-link files with ffmpeg.
"""
import os
import uuid
import asyncio
import subprocess
import yt_dlp

from config import DOWNLOAD_DIR


class DownloadResult:
    def __init__(self, filepath, title, thumbnail_url, duration, uploader=None):
        self.filepath = filepath
        self.title = title
        self.thumbnail_url = thumbnail_url
        self.duration = duration  # seconds
        self.uploader = uploader


def _job_dir():
    d = os.path.join(DOWNLOAD_DIR, uuid.uuid4().hex[:10])
    os.makedirs(d, exist_ok=True)
    return d


def _run_ytdlp(url: str, audio_only: bool) -> DownloadResult:
    job_dir = _job_dir()
    out_tmpl = os.path.join(job_dir, "%(id)s.%(ext)s")

    if audio_only:
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": out_tmpl,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }
    else:
        ydl_opts = {
            # cap at 1080p so files stay a reasonable size for Telegram uploads
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
            filepath = os.path.splitext(filepath)[0] + ".mp3"

    return DownloadResult(
        filepath=filepath,
        title=info.get("title", "Unknown title"),
        thumbnail_url=info.get("thumbnail"),
        duration=info.get("duration") or 0,
        uploader=info.get("uploader"),
    )


async def download_youtube_video(url: str) -> DownloadResult:
    return await asyncio.to_thread(_run_ytdlp, url, False)


async def download_youtube_music(url: str) -> DownloadResult:
    return await asyncio.to_thread(_run_ytdlp, url, True)


def _run_direct_download(url: str) -> DownloadResult:
    """Download a direct audio/video file link (non-YouTube) with yt-dlp's
    generic extractor, which also handles plain http(s) file links."""
    job_dir = _job_dir()
    out_tmpl = os.path.join(job_dir, "file.%(ext)s")
    ydl_opts = {
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
        thumbnail_url=info.get("thumbnail"),
        duration=info.get("duration") or 0,
    )


async def download_direct_file(url: str) -> DownloadResult:
    return await asyncio.to_thread(_run_direct_download, url)


def probe_duration(filepath: str) -> float:
    """Get duration in seconds using ffprobe."""
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", filepath
        ])
        return float(out.strip())
    except Exception:
        return 0.0


def trim_clip(filepath: str, start_seconds: float, out_ext: str) -> str:
    """Best-effort 'seek': cut a new clip starting at start_seconds to the end.
    Used because Telegram cannot remote-control playback of an already-sent file —
    seeking re-uploads a trimmed copy starting at the requested position."""
    out_path = os.path.join(
        os.path.dirname(filepath),
        f"seek_{int(start_seconds)}_{os.path.basename(filepath)}"
    )
    cmd = [
        "ffmpeg", "-y", "-ss", str(start_seconds), "-i", filepath,
        "-c", "copy", out_path
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out_path


def cleanup_job(filepath: str):
    """Remove the whole job directory a file belongs to."""
    job_dir = os.path.dirname(filepath)
    try:
        for f in os.listdir(job_dir):
            os.remove(os.path.join(job_dir, f))
        os.rmdir(job_dir)
    except Exception:
        pass
