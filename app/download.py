"""Download videos from YouTube, TikTok, X, Vimeo, Twitch, direct .mp4 links, etc. via yt-dlp."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

import yt_dlp

from .config import load_settings


class DownloadError(RuntimeError):
    pass


def _js_runtimes() -> dict:
    # YouTube needs a JavaScript runtime to unlock its video formats.
    runtimes = {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}
    return runtimes or {"deno": {}}


def download(url: str, out_dir: Path, on_progress: Callable[[float, str], None],
             cancelled: Callable[[], bool]) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    def hook(d):
        if cancelled():
            raise DownloadError("Cancelled")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            frac = done / total if total else 0.0
            speed = d.get("speed") or 0
            msg = f"Downloading… {done / 1e6:.0f} MB" + (f" of {total / 1e6:.0f} MB" if total else "")
            if speed:
                msg += f" ({speed / 1e6:.1f} MB/s)"
            on_progress(frac, msg)

    opts = {
        # Prefer H.264 MP4 up to 1080p, fall back to anything available.
        "format": "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "js_runtimes": _js_runtimes(),
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
    }
    browser = load_settings().get("cookies_from_browser", "").strip()
    if browser:
        opts["cookiesfrombrowser"] = (browser,)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError:
        raise
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).replace("ERROR: ", "")
        if "Sign in to confirm" in msg or "not a bot" in msg:
            msg += ("\n\nTip: open Settings and set 'Use cookies from browser' to the browser "
                    "where you are logged in to YouTube.")
        raise DownloadError(msg[:800]) from None

    if info and info.get("_type") == "playlist":
        entries = [e for e in info.get("entries") or [] if e]
        info = entries[0] if entries else info

    files = sorted(p for p in out_dir.glob("source.*") if p.suffix not in (".part", ".ytdl"))
    if not files:
        raise DownloadError("Download finished but no video file was produced.")
    return {
        "path": str(max(files, key=lambda p: p.stat().st_size)),
        "title": (info or {}).get("title") or "Untitled video",
        "description": ((info or {}).get("description") or "")[:2000],
        "uploader": (info or {}).get("uploader") or "",
    }
