"""Speech-to-text with word-level timestamps (faster-whisper, runs locally)."""
from __future__ import annotations

import threading
import wave
from pathlib import Path
from typing import Callable

import numpy as np

_models: dict[str, object] = {}
_lock = threading.Lock()


def _load(name: str):
    from faster_whisper import WhisperModel
    import ctranslate2

    with _lock:
        if name not in _models:
            if ctranslate2.get_cuda_device_count() > 0:
                _models[name] = WhisperModel(name, device="cuda", compute_type="float16")
            else:
                _models[name] = WhisperModel(name, device="cpu", compute_type="int8")
        return _models[name]


def transcribe(audio: Path, model_name: str, language: str | None, duration: float,
               on_progress: Callable[[float, str], None], cancelled: Callable[[], bool]) -> dict:
    on_progress(0.0, f"Loading speech model ({model_name})… first run downloads it")
    model = _load(model_name)
    # Load the 16 kHz mono WAV ourselves (avoids PyAV version incompatibilities).
    with wave.open(str(audio), "rb") as wf:
        samples = np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(np.float32) / 32768.0
    segments, info = model.transcribe(
        samples,
        language=language or None,
        word_timestamps=True,
        vad_filter=True,
        beam_size=5,
        condition_on_previous_text=False,
    )
    out = []
    for seg in segments:
        if cancelled():
            raise RuntimeError("Cancelled")
        words = [
            {"w": w.word.strip(), "s": round(float(w.start), 3), "e": round(float(w.end), 3)}
            for w in (seg.words or []) if w.word.strip()
        ]
        if words:
            out.append({"s": words[0]["s"], "e": words[-1]["e"], "text": seg.text.strip(), "words": words})
        on_progress(min(seg.end / max(duration, 1e-6), 1.0),
                    f"Transcribing… {int(seg.end // 60)}:{int(seg.end % 60):02d} / "
                    f"{int(duration // 60)}:{int(duration % 60):02d}")
    return {"language": info.language, "segments": out}
