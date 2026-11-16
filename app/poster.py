"""Background queue that posts clips to TikTok at their scheduled times and tracks their status."""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import tiktok
from .config import DATA_DIR, JOBS_DIR

POSTS_FILE = DATA_DIR / "tiktok_posts.json"
ACTIVE = ("scheduled", "uploading", "processing")
CHECK_EVERY = 20          # seconds between status checks while TikTok processes a post
GIVE_UP_AFTER = 45 * 60   # stop polling a post that is still processing after this long


class Poster:
    def __init__(self):
        self.lock = threading.RLock()
        self.posts: list[dict] = []
        self._wake = threading.Event()
        self._load()
        threading.Thread(target=self._loop, daemon=True, name="tiktok-poster").start()

    # ------------------------------------------------------------------ storage
    def _load(self):
        try:
            self.posts = json.loads(POSTS_FILE.read_text("utf-8"))
        except (OSError, ValueError):
            self.posts = []
        for p in self.posts:
            if p["status"] == "uploading":  # the app was closed mid-upload
                p.update(status="failed", error="Interrupted because the app was closed. Click Retry.")
        self._save()

    def _save(self):
        tmp = POSTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.posts, indent=1), "utf-8")
        tmp.replace(POSTS_FILE)

    def _update(self, p: dict, **fields):
        with self.lock:
            p.update(fields, updated_at=time.time())
            self._save()

    # ------------------------------------------------------------------ API
    def list(self) -> list[dict]:
        with self.lock:
            return json.loads(json.dumps(sorted(self.posts, key=lambda p: p["scheduled_at"])))

    def create(self, items: list[dict], settings: dict, start_at: float, interval_min: float) -> list[dict]:
        created = []
        with self.lock:
            for n, item in enumerate(items):
                p = {
                    "id": uuid.uuid4().hex[:12], "job_id": item["job_id"], "clip_index": item["index"],
                    "clip_title": item.get("clip_title", ""), "title": item.get("title", ""),
                    "mode": settings["mode"], "privacy_level": settings.get("privacy_level"),
                    "allow_comment": settings.get("allow_comment", False),
                    "allow_duet": settings.get("allow_duet", False),
                    "allow_stitch": settings.get("allow_stitch", False),
                    "brand_organic": settings.get("brand_organic", False),
                    "branded_content": settings.get("branded_content", False),
                    "scheduled_at": start_at + n * interval_min * 60,
                    "status": "scheduled", "progress": 0.0, "publish_id": None, "error": "",
                    "post_ids": [], "created_at": time.time(), "updated_at": time.time(),
                }
                self.posts.append(p)
                created.append(p)
            self._save()
        self._wake.set()
        return json.loads(json.dumps(created))

    def cancel(self, post_id: str) -> bool:
        with self.lock:
            p = self._find(post_id)
            if p and p["status"] == "scheduled":
                self._update(p, status="cancelled")
                return True
        return False

    def retry(self, post_id: str) -> bool:
        with self.lock:
            p = self._find(post_id)
            if p and p["status"] in ("failed", "cancelled"):
                self._update(p, status="scheduled", scheduled_at=time.time(), error="", progress=0.0,
                             publish_id=None)
                self._wake.set()
                return True
        return False

    def remove(self, post_id: str) -> None:
        with self.lock:
            self.posts = [p for p in self.posts if not (p["id"] == post_id and p["status"] not in ACTIVE[1:])]
            self._save()

    def _find(self, post_id: str) -> dict | None:
        return next((p for p in self.posts if p["id"] == post_id), None)

    # ------------------------------------------------------------------ worker
    def _loop(self):
        while True:
            self._wake.wait(timeout=5)
            self._wake.clear()
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - never let the worker die
                traceback.print_exc()

    def _tick(self):
        now = time.time()
        with self.lock:
            due = sorted((p for p in self.posts if p["status"] == "scheduled" and p["scheduled_at"] <= now),
                         key=lambda p: p["scheduled_at"])
            checks = [p for p in self.posts if p["status"] == "processing" and p.get("next_check", 0) <= now]
        if due:
            self._publish(due[0])  # one at a time: TikTok allows 6 requests per minute per account
        for p in checks:
            self._check(p)

    def _publish(self, p: dict):
        path = JOBS_DIR / p["job_id"] / "clips" / f"clip_{p['clip_index']:02d}.mp4"
        if not path.exists():
            self._update(p, status="failed", error="The clip file no longer exists (was the video deleted?).")
            return
        self._update(p, status="uploading", progress=0.0, error="")
        try:
            info = tiktok.creator_info()  # TikTok requires fresh creator info before every post
            duration = _duration(p)
            max_dur = info.get("max_video_post_duration_sec")
            if max_dur and duration and duration > max_dur:
                raise tiktok.TikTokError("too_long", f"This account can post videos up to {max_dur} s; "
                                                     f"the clip is {duration:.0f} s.")
            progress = lambda f: self._update(p, progress=round(f * 100, 1))  # noqa: E731
            if p["mode"] == "draft":
                publish_id = tiktok.upload_to_drafts(path, progress)
            else:
                if p["privacy_level"] not in (info.get("privacy_level_options") or []):
                    raise tiktok.TikTokError("privacy_level_option_mismatch")
                post_info = {
                    "title": p["title"][:2200],
                    "privacy_level": p["privacy_level"],
                    "disable_comment": not p["allow_comment"] or bool(info.get("comment_disabled")),
                    "disable_duet": not p["allow_duet"] or bool(info.get("duet_disabled")),
                    "disable_stitch": not p["allow_stitch"] or bool(info.get("stitch_disabled")),
                    "video_cover_timestamp_ms": 1000,
                    "brand_organic_toggle": bool(p["brand_organic"]),
                    "brand_content_toggle": bool(p["branded_content"]),
                }
                publish_id = tiktok.direct_post(path, post_info, progress)
            self._update(p, status="processing", publish_id=publish_id, progress=100.0,
                         posted_at=time.time(), next_check=time.time() + 10)
        except tiktok.TikTokError as e:
            if e.code == "rate_limit_exceeded":
                self._update(p, status="scheduled", scheduled_at=time.time() + 65, error="")
            else:
                self._update(p, status="failed", error=str(e), error_code=e.code)

    def _check(self, p: dict):
        try:
            st = tiktok.fetch_status(p["publish_id"])
        except tiktok.TikTokError as e:
            if e.code in ("rate_limit_exceeded", "network", "bad_response"):
                self._update(p, next_check=time.time() + 60)
            else:
                self._update(p, status="failed", error=str(e), error_code=e.code)
            return
        status = st.get("status", "")
        if status == "PUBLISH_COMPLETE":
            self._update(p, status="posted", post_ids=st.get("publicaly_available_post_id") or [])
        elif status == "SEND_TO_USER_INBOX":
            self._update(p, status="in_drafts")
        elif status == "FAILED":
            reason = st.get("fail_reason", "unknown")
            self._update(p, status="failed", error=str(tiktok.TikTokError(reason)), error_code=reason)
        elif time.time() - p.get("posted_at", time.time()) > GIVE_UP_AFTER:
            self._update(p, status="posted_unconfirmed")
        else:
            self._update(p, next_check=time.time() + CHECK_EVERY)


def _duration(p: dict) -> float | None:
    try:
        job = json.loads((JOBS_DIR / p["job_id"] / "job.json").read_text("utf-8"))
        clip = next(c for c in job["clips"] if c["index"] == p["clip_index"])
        return float(clip["duration"])
    except (OSError, ValueError, StopIteration, KeyError):
        return None
