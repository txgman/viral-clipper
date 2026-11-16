"""Paths and user settings."""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)  # running as the packaged .exe
BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
if FROZEN:
    # Program Files isn't writable; keep videos/clips/settings in the user's AppData.
    _default_data = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ViralClipper"
    # FFmpeg, FFprobe and Deno ship in the bin folder next to the .exe
    _bin = Path(sys.executable).resolve().parent / "bin"
    if _bin.is_dir():
        os.environ["PATH"] = str(_bin) + os.pathsep + os.environ.get("PATH", "")
else:
    _default_data = BASE_DIR / "data"
DATA_DIR = Path(os.environ.get("CLIPPER_DATA_DIR", _default_data)).resolve()
JOBS_DIR = DATA_DIR / "jobs"
SETTINGS_FILE = DATA_DIR / "settings.json"
FONT_PATH = APP_DIR / "assets" / "fonts" / "Inter-Black.otf"
CASCADE_DIR = APP_DIR / "assets" / "haarcascades"

JOBS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS = {
    # Claude API key. The ANTHROPIC_API_KEY environment variable wins if set.
    "anthropic_api_key": "",
    "claude_model": "claude-opus-5-5",
    # faster-whisper model: tiny, base, small, medium, large-v3, or a local folder path.
    "whisper_model": os.environ.get("CLIPPER_WHISPER_MODEL", "small"),
    # Browser to borrow YouTube cookies from when YouTube asks you to sign in
    # ("chrome", "firefox", "edge", "safari", "brave"...). Empty = no cookies.
    "cookies_from_browser": "",
    # Your own TikTok developer app (developers.tiktok.com) for auto-posting. Stays on this computer.
    "tiktok_client_key": "",
    "tiktok_client_secret": "",
}

_lock = threading.Lock()


def load_settings() -> dict:
    with _lock:
        data = dict(DEFAULT_SETTINGS)
        if SETTINGS_FILE.exists():
            try:
                data.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
            except (OSError, ValueError):
                pass
        return data


def save_settings(updates: dict) -> dict:
    current = load_settings()
    for key in DEFAULT_SETTINGS:
        if key in updates and updates[key] is not None:
            current[key] = str(updates[key]).strip()
    with _lock:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, indent=2), "utf-8")
        tmp.replace(SETTINGS_FILE)
    return current


def anthropic_api_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY") or load_settings().get("anthropic_api_key", "")
