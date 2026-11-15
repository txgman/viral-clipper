"""Web server: `python -m app` or `uvicorn app.main:app`."""
from __future__ import annotations

import io
import re
import shutil
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .jobs import JobStore

app = FastAPI(title="Viral Clipper")
store = JobStore()
STATIC = config.APP_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

ALLOWED_UPLOAD = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg", ".ts"}


def _job_or_404(job_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(404, "Job not found")
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


def _slug(text: str, n: int = 50) -> str:
    s = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)[:n].strip("-") or "clip"


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text("utf-8")


@app.get("/api/status")
def status():
    settings = config.load_settings()
    js = [n for n in ("deno", "node", "bun") if shutil.which(n)]
    return {
        "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        "ai": bool(config.anthropic_api_key()),
        "ai_from_env": bool(__import__("os").environ.get("ANTHROPIC_API_KEY")),
        "js_runtime": js[0] if js else None,
        "whisper_model": settings["whisper_model"],
    }


@app.post("/api/jobs")
async def create_job(
    url: str = Form(""),
    file: UploadFile | None = File(None),
    count: int = Form(5),
    min_len: float = Form(20),
    max_len: float = Form(60),
    layout: str = Form("auto"),
    captions: str = Form("yellow"),
    language: str = Form(""),
):
    if not (1 <= count <= 15):
        raise HTTPException(400, "Number of clips must be between 1 and 15.")
    if not (5 <= min_len < max_len <= 180):
        raise HTTPException(400, "Clip length must be between 5 and 180 seconds, with min below max.")
    if layout not in ("auto", "track", "fit", "center"):
        raise HTTPException(400, "Unknown layout.")
    if captions not in ("yellow", "green", "white", "none"):
        raise HTTPException(400, "Unknown caption style.")
    options = {"count": count, "min_len": min_len, "max_len": max_len, "layout": layout,
               "captions": captions, "language": language.strip()[:8]}

    url = url.strip()
    if file is not None and file.filename:
        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_UPLOAD:
            raise HTTPException(400, f"Unsupported file type {ext or '(none)'}. Upload a video file.")
        job = store.create("file", file.filename, options, title=Path(file.filename).stem)
        dest = config.JOBS_DIR / job["id"] / f"source{ext}"
        with dest.open("wb") as fh:
            while chunk := await file.read(8 * 1024 * 1024):
                fh.write(chunk)
    elif url:
        if not re.match(r"^https?://", url, re.I):
            raise HTTPException(400, "Please paste a full link starting with http:// or https://")
        job = store.create("url", url, options)
    else:
        raise HTTPException(400, "Paste a video link or choose a file to upload.")
    store.enqueue(job["id"])
    return job


@app.get("/api/jobs")
def list_jobs():
    return store.list()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _job_or_404(job_id)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    _job_or_404(job_id)
    store.cancel(job_id)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    _job_or_404(job_id)
    store.delete(job_id)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/clips/{index}/{kind}")
def clip_file(job_id: str, index: int, kind: str, download: bool = False):
    job = _job_or_404(job_id)
    clip = next((c for c in job["clips"] if c["index"] == index), None)
    if not clip or clip.get("status") != "done":
        raise HTTPException(404, "Clip not ready")
    clip_dir = config.JOBS_DIR / job_id / "clips"
    if kind == "video":
        name = f"{index:02d}-{_slug(clip['title'])}.mp4"
        return FileResponse(clip_dir / clip["file"], media_type="video/mp4",
                            filename=name if download else None,
                            content_disposition_type="attachment" if download else "inline")
    if kind == "thumb":
        return FileResponse(clip_dir / clip["thumb"], media_type="image/jpeg")
    raise HTTPException(404)


@app.get("/api/jobs/{job_id}/zip")
def zip_job(job_id: str):
    job = _job_or_404(job_id)
    clip_dir = config.JOBS_DIR / job_id / "clips"
    done = [c for c in job["clips"] if c.get("status") == "done"]
    if not done:
        raise HTTPException(404, "No finished clips yet")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        notes = []
        for c in done:
            name = f"{c['index']:02d}-{_slug(c['title'])}.mp4"
            zf.write(clip_dir / c["file"], name)
            notes.append(f"{name}\nViral score: {c['score']}\nTitle: {c['title']}\n"
                         f"Caption: {c['caption']}\nHashtags: {' '.join(c['hashtags'])}\n"
                         f"Why: {c['reason']}\n")
        zf.writestr("captions-and-hashtags.txt", "\n".join(notes))
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{_slug(job["title"])}-clips.zip"'})


class SettingsIn(BaseModel):
    anthropic_api_key: str | None = None
    claude_model: str | None = None
    whisper_model: str | None = None
    cookies_from_browser: str | None = None


def _public_settings(s: dict) -> dict:
    key = s.get("anthropic_api_key", "")
    out = {k: v for k, v in s.items() if k != "anthropic_api_key"}
    out["anthropic_api_key_set"] = bool(key)
    out["anthropic_api_key_hint"] = f"…{key[-4:]}" if len(key) > 8 else ""
    return out


@app.get("/api/settings")
def get_settings():
    return _public_settings(config.load_settings())


@app.post("/api/settings")
def update_settings(body: SettingsIn):
    data = body.model_dump()
    if data.get("whisper_model") is not None and not data["whisper_model"].strip():
        data["whisper_model"] = None
    return _public_settings(config.save_settings(data))
