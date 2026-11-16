"""Web server: `python -m app` or `uvicorn app.main:app`."""
from __future__ import annotations

import io
import re
import shutil
import zipfile
from pathlib import Path

import time
import urllib.parse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, selector, tiktok
from .jobs import JobStore
from .poster import Poster

app = FastAPI(title="Viral Clipper")
store = JobStore()
poster = Poster()
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
    tiktok_client_key: str | None = None
    tiktok_client_secret: str | None = None


def _public_settings(s: dict) -> dict:
    key = s.get("anthropic_api_key", "")
    out = {k: v for k, v in s.items() if k not in ("anthropic_api_key", "tiktok_client_secret")}
    out["anthropic_api_key_set"] = bool(key)
    out["anthropic_api_key_hint"] = f"…{key[-4:]}" if len(key) > 8 else ""
    out["tiktok_client_secret_set"] = bool(s.get("tiktok_client_secret"))
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


# --------------------------------------------------------------------------- TikTok

@app.get("/api/tiktok/status")
def tiktok_status(request: Request):
    port = request.url.port or 80
    return {
        "configured": tiktok.configured(),
        "account": tiktok.public_account(),
        "redirect_uri": f"http://127.0.0.1:*{tiktok.CALLBACK_PATH}",
        "this_redirect_uri": f"http://127.0.0.1:{port}{tiktok.CALLBACK_PATH}",
    }


@app.get("/api/tiktok/connect")
def tiktok_connect(request: Request):
    if not tiktok.configured():
        raise HTTPException(400, "Add your TikTok Client key and Client secret first.")
    redirect_uri = f"http://127.0.0.1:{request.url.port or 80}{tiktok.CALLBACK_PATH}"
    return RedirectResponse(tiktok.authorize_url(redirect_uri), status_code=302)


@app.get(tiktok.CALLBACK_PATH)
def tiktok_callback(code: str = "", state: str = "", error: str = "", error_description: str = ""):
    def back(**q):
        return RedirectResponse("/?" + urllib.parse.urlencode(q), status_code=302)
    if error:
        return back(tiktok_error=error_description or error)
    try:
        tiktok.complete_login(state, code)
    except tiktok.TikTokError as e:
        return back(tiktok_error=str(e))
    return back(tiktok="connected")


@app.post("/api/tiktok/disconnect")
def tiktok_disconnect():
    tiktok.disconnect()
    return {"ok": True}


def _tiktok_call(fn, *args):
    try:
        return fn(*args)
    except tiktok.NotConnected as e:
        raise HTTPException(401, str(e)) from None
    except tiktok.TikTokError as e:
        raise HTTPException(502, str(e)) from None


@app.get("/api/tiktok/creator")
def tiktok_creator():
    """Fresh creator info: TikTok requires showing it on the post screen every time."""
    return _tiktok_call(tiktok.creator_info)


class CaptionIn(BaseModel):
    job_id: str
    index: int


@app.post("/api/tiktok/captions")
def tiktok_captions(body: CaptionIn):
    job = _job_or_404(body.job_id)
    clip = next((c for c in job["clips"] if c["index"] == body.index), None)
    if not clip:
        raise HTTPException(404, "Clip not found")
    key = config.anthropic_api_key()
    if not key:
        raise HTTPException(400, "Smart captions use Claude. Add your Anthropic API key in Settings first.")
    try:
        options = selector.tiktok_captions(clip, job["title"], key,
                                           config.load_settings().get("claude_model") or "claude-opus-5-5")
    except selector.AIError as e:
        raise HTTPException(502, str(e)) from None
    return {"options": options}


class PostItem(BaseModel):
    job_id: str
    index: int
    title: str = ""


class PostsIn(BaseModel):
    items: list[PostItem]
    mode: str  # "direct" or "draft"
    privacy_level: str | None = None
    allow_comment: bool = False
    allow_duet: bool = False
    allow_stitch: bool = False
    brand_organic: bool = False
    branded_content: bool = False
    consent: bool = False
    start_at: float | None = None  # unix seconds; None = now
    interval_minutes: float = 0


@app.post("/api/tiktok/posts")
def create_posts(body: PostsIn):
    acc = tiktok.public_account()
    if not acc:
        raise HTTPException(401, "Connect your TikTok account first.")
    if not body.consent:
        raise HTTPException(400, "You need to confirm before posting.")
    if body.mode not in ("direct", "draft"):
        raise HTTPException(400, "Unknown posting mode.")
    if body.mode == "direct":
        if not acc["can_post"]:
            raise HTTPException(400, "TikTok didn't grant direct posting (video.publish). Reconnect TikTok.")
        if not body.privacy_level:
            raise HTTPException(400, "Choose who can view the post.")
        if body.branded_content and body.privacy_level == "SELF_ONLY":
            raise HTTPException(400, "Branded content visibility cannot be set to private.")
    elif not acc["can_draft"]:
        raise HTTPException(400, "TikTok didn't grant draft uploads (video.upload). Reconnect TikTok.")
    if not 1 <= len(body.items) <= 30:
        raise HTTPException(400, "Choose between 1 and 30 clips.")
    if not 0 <= body.interval_minutes <= 7 * 24 * 60:
        raise HTTPException(400, "Spacing between posts must be at most a week.")
    items = []
    for it in body.items:
        job = _job_or_404(it.job_id)
        clip = next((c for c in job["clips"] if c["index"] == it.index and c.get("status") == "done"), None)
        if not clip:
            raise HTTPException(400, f"Clip {it.index} isn't ready.")
        if len(it.title.encode("utf-16-le")) // 2 > 2200:
            raise HTTPException(400, f"The caption for clip {it.index} is longer than 2,200 characters.")
        items.append({"job_id": it.job_id, "index": it.index, "title": it.title, "clip_title": clip["title"]})
    start = body.start_at if body.start_at and body.start_at > time.time() else time.time()
    settings = body.model_dump(exclude={"items", "start_at", "interval_minutes", "consent"})
    return poster.create(items, settings, start, body.interval_minutes)


@app.get("/api/tiktok/posts")
def list_posts():
    return poster.list()


@app.post("/api/tiktok/posts/{post_id}/cancel")
def cancel_post(post_id: str):
    if not poster.cancel(post_id):
        raise HTTPException(400, "Only scheduled posts can be cancelled.")
    return {"ok": True}


@app.post("/api/tiktok/posts/{post_id}/retry")
def retry_post(post_id: str):
    if not poster.retry(post_id):
        raise HTTPException(400, "Only failed or cancelled posts can be retried.")
    return {"ok": True}


@app.delete("/api/tiktok/posts/{post_id}")
def remove_post(post_id: str):
    poster.remove(post_id)
    return {"ok": True}
