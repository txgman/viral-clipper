"""Job store + background worker that runs the full pipeline."""
from __future__ import annotations

import json
import queue
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import media, render, selector, transcribe
from .config import JOBS_DIR, anthropic_api_key, load_settings
from .download import DownloadError, download

ACTIVE = ("queued", "running")


class Cancelled(Exception):
    pass


class JobStore:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.queue: "queue.Queue[str]" = queue.Queue()
        self.cancel_flags: set[str] = set()
        self.delete_after: set[str] = set()
        self._last_save: dict[str, float] = {}
        self._load()
        threading.Thread(target=self._worker, daemon=True, name="clip-worker").start()

    # ------------------------------------------------------------------ persistence
    def _load(self):
        for f in sorted(JOBS_DIR.glob("*/job.json")):
            try:
                job = json.loads(f.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if job.get("status") == "running":
                job.update(status="error", error="Interrupted because the server was restarted. Run it again.")
            self.jobs[job["id"]] = job
            if job.get("status") == "queued":
                self.queue.put(job["id"])

    def _save(self, job: dict, force: bool = False):
        now = time.time()
        if not force and now - self._last_save.get(job["id"], 0) < 1.0:
            return
        self._last_save[job["id"]] = now
        d = JOBS_DIR / job["id"]
        if not d.exists():
            return
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(job, indent=1), "utf-8")
        tmp.replace(d / "job.json")

    def update(self, job_id: str, force: bool = False, **fields):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job.update(fields)
            job["updated_at"] = time.time()
            self._save(job, force=force)

    # ------------------------------------------------------------------ public API
    def create(self, source_type: str, source: str, options: dict, title: str = "") -> dict:
        job_id = uuid.uuid4().hex[:12]
        (JOBS_DIR / job_id / "clips").mkdir(parents=True, exist_ok=True)
        job = {
            "id": job_id, "created_at": time.time(), "updated_at": time.time(),
            "source_type": source_type, "source": source, "options": options,
            "status": "queued", "stage": "Waiting in line…", "progress": 0.0,
            "title": title or source, "duration": None, "clips": [], "ai_mode": None, "ai_note": "",
            "error": "",
        }
        with self.lock:
            self.jobs[job_id] = job
            self._save(job, force=True)
        return job

    def enqueue(self, job_id: str):
        self.queue.put(job_id)

    def list(self) -> list[dict]:
        with self.lock:
            return [json.loads(json.dumps(j)) for j in
                    sorted(self.jobs.values(), key=lambda j: -j["created_at"])]

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return json.loads(json.dumps(job)) if job else None

    def cancel(self, job_id: str):
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job["status"] in ACTIVE:
                self.cancel_flags.add(job_id)
                if job["status"] == "queued":
                    self.update(job_id, force=True, status="cancelled", stage="Cancelled")

    def delete(self, job_id: str):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            if job["status"] == "running":
                self.cancel_flags.add(job_id)
                self.delete_after.add(job_id)
                return
            self.cancel_flags.add(job_id)
            self.jobs.pop(job_id, None)
        shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)

    # ------------------------------------------------------------------ worker
    def _worker(self):
        while True:
            job_id = self.queue.get()
            job = self.get(job_id)
            if not job or job["status"] != "queued" or job_id in self.cancel_flags:
                continue
            self.update(job_id, force=True, status="running", stage="Starting…", progress=0.0)
            try:
                self._run(job_id, job)
                self.update(job_id, force=True, status="done", stage="Done", progress=100.0)
            except Cancelled:
                self.update(job_id, force=True, status="cancelled", stage="Cancelled")
            except Exception as e:  # noqa: BLE001 - surface any failure to the UI
                if job_id in self.cancel_flags:
                    self.update(job_id, force=True, status="cancelled", stage="Cancelled")
                else:
                    traceback.print_exc()
                    self.update(job_id, force=True, status="error", stage="Failed", error=str(e) or repr(e))
            if job_id in self.delete_after:
                self.delete_after.discard(job_id)
                with self.lock:
                    self.jobs.pop(job_id, None)
                shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)

    def _run(self, job_id: str, job: dict):
        jd = JOBS_DIR / job_id
        opts = job["options"]
        settings = load_settings()

        def cancelled():
            return job_id in self.cancel_flags

        def check():
            if cancelled():
                raise Cancelled()

        def stage(lo, hi):
            def cb(frac, msg=None):
                fields = {"progress": round(lo + (hi - lo) * max(0.0, min(frac, 1.0)), 1)}
                if msg:
                    fields["stage"] = msg
                self.update(job_id, **fields)
            return cb

        media.require_ffmpeg()

        # 1. Get the video -------------------------------------------------
        if job["source_type"] == "url":
            self.update(job_id, stage="Downloading video…")
            try:
                info = download(job["source"], jd, stage(0, 15), cancelled)
            except DownloadError as e:
                if cancelled():
                    raise Cancelled() from None
                raise RuntimeError(f"Could not download the video: {e}") from None
            src = Path(info["path"])
            title, description = info["title"], info["description"]
        else:
            src = next(p for p in jd.glob("source.*"))
            title, description = job["title"], ""
        check()

        probe = media.probe(src)
        self.update(job_id, force=True, title=title, duration=probe["duration"],
                    video={"width": probe["width"], "height": probe["height"]})

        # 2. Transcribe ----------------------------------------------------
        segments, curve = [], None
        if probe["has_audio"]:
            self.update(job_id, stage="Extracting audio…", progress=15)
            wav = jd / "audio.wav"
            media.extract_audio(src, wav)
            curve = media.loudness_curve(wav, selector.HOP)
            check()
            tr = transcribe.transcribe(wav, settings["whisper_model"], opts.get("language") or None,
                                       probe["duration"], stage(16, 55), cancelled)
            segments = tr["segments"]
            (jd / "transcript.json").write_text(json.dumps(tr), "utf-8")
            wav.unlink(missing_ok=True)
        check()
        import numpy as np
        if curve is None:
            curve = np.zeros(0, dtype=np.float32)

        # 3. Find viral moments --------------------------------------------
        self.update(job_id, force=True, stage="Finding the most viral moments…", progress=56)
        sents = selector.build_sentences(segments)
        duration = probe["duration"]
        hi = min(float(opts["max_len"]), duration)
        lo = min(float(opts["min_len"]), hi * 0.6)
        count = int(opts["count"])
        key = anthropic_api_key()
        ai_mode, note = "heuristic", ""
        clips = []
        if key and sents:
            self.update(job_id, stage="Claude is watching the transcript for viral moments…")
            try:
                clips = selector.claude_clips(sents, curve, duration, count, lo, hi, title, description,
                                              key, settings.get("claude_model") or "claude-opus-5-5")
                ai_mode = "claude"
            except selector.AIError as e:
                note = f"Claude was unavailable ({e}) — used built-in scoring instead."
        elif not key:
            note = "No Anthropic API key set — used built-in scoring. Add a key in Settings for much smarter picks."
        else:
            note = "No speech found — picked the most energetic moments."
        if not clips:
            clips = selector.heuristic_clips(sents, curve, duration, count, lo, hi)
        check()

        all_words = [w for seg in segments for w in seg["words"]]
        records = [{
            "index": n + 1, **c, "duration": round(c["end"] - c["start"], 2),
            "file": f"clip_{n + 1:02d}.mp4", "thumb": f"clip_{n + 1:02d}.jpg",
            "status": "pending", "layout": None,
            "transcript": selector.join_words([w for w in all_words if c["start"] <= w["s"] < c["end"]]),
        } for n, c in enumerate(clips)]
        self.update(job_id, force=True, clips=records, ai_mode=ai_mode, ai_note=note, progress=62)

        # 4. Render vertical clips -----------------------------------------
        clip_dir = jd / "clips"
        span = 38.0 / max(len(records), 1)
        for n, rec in enumerate(records):
            check()
            base = 62 + span * n
            self.update(job_id, stage=f"Rendering clip {n + 1} of {len(records)}…", progress=round(base, 1))
            rec["status"] = "rendering"
            self.update(job_id, clips=records)
            out = clip_dir / rec["file"]
            try:
                info = render.render_clip(
                    src, out, rec["start"], rec["end"], all_words, probe,
                    opts.get("layout", "auto"), opts.get("captions", "yellow"), jd,
                    lambda f, base=base: self.update(job_id, progress=round(base + span * f, 1)),
                    cancelled)
            except render.RenderError:
                if cancelled():
                    raise Cancelled() from None
                raise
            media.thumbnail(out, clip_dir / rec["thumb"], at=min(1.0, rec["duration"] / 2))
            rec.update(status="done", layout=info["layout"], size=out.stat().st_size)
            self.update(job_id, force=True, clips=records)

        if not opts.get("keep_source"):
            for p in jd.glob("source.*"):
                p.unlink(missing_ok=True)
