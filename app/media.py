"""ffmpeg / ffprobe helpers and audio loudness analysis."""
from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np


class MediaError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise MediaError(f"{tool} is not installed or not on PATH. Install FFmpeg first (see README).")


def probe(path: Path) -> dict:
    require_ffmpeg()
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise MediaError(f"Could not read the video file: {proc.stderr.strip()[:300]}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    video = next(
        (s for s in streams if s.get("codec_type") == "video"
         and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    if video is None:
        raise MediaError("The file has no video stream.")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    width, height = int(video["width"]), int(video["height"])
    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(float(sd["rotation"]))
    if not rotation and "rotate" in video.get("tags", {}):
        rotation = int(float(video["tags"]["rotate"]))
    if abs(rotation) % 180 == 90:
        width, height = height, width

    sar = video.get("sample_aspect_ratio", "1:1")
    try:
        num, den = (int(x) for x in sar.split(":"))
        if num > 0 and den > 0 and num != den:
            width = int(round(width * num / den))
    except ValueError:
        pass

    duration = 0.0
    for candidate in (data.get("format", {}).get("duration"), video.get("duration")):
        try:
            duration = float(candidate)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue
    if duration <= 0:
        raise MediaError("Could not determine the video length.")

    fps = 30.0
    try:
        n, d = video.get("avg_frame_rate", "30/1").split("/")
        if float(d) > 0 and float(n) > 0:
            fps = float(n) / float(d)
    except ValueError:
        pass

    return {"width": width - width % 2, "height": height - height % 2, "duration": duration,
            "fps": fps, "has_audio": has_audio}


def extract_audio(src: Path, out_wav: Path) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(out_wav)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise MediaError(f"Audio extraction failed: {proc.stderr.strip()[:300]}")


def loudness_curve(wav_path: Path, hop: float = 0.5) -> np.ndarray:
    """RMS loudness in dBFS for consecutive `hop`-second windows."""
    with wave.open(str(wav_path), "rb") as wf:
        rate = wf.getframerate()
        hop_frames = int(rate * hop)
        values = []
        while True:
            raw = wf.readframes(hop_frames * 240)  # ~2 minutes per read
            if not raw:
                break
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            n = len(samples) // hop_frames
            if n:
                blocks = samples[: n * hop_frames].reshape(n, hop_frames)
                values.append(np.sqrt(np.mean(blocks ** 2, axis=1) + 1e-10))
            rest = samples[n * hop_frames:]
            if len(rest):
                values.append(np.array([np.sqrt(np.mean(rest ** 2) + 1e-10)], dtype=np.float32))
    if not values:
        return np.zeros(0, dtype=np.float32)
    return (20 * np.log10(np.concatenate(values))).astype(np.float32)


def thumbnail(video: Path, out_jpg: Path, at: float = 1.0) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1",
         "-vf", "scale=360:-2", "-q:v", "4", str(out_jpg)],
        capture_output=True,
    )
