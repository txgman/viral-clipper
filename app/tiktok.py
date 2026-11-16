"""TikTok Login Kit (desktop, PKCE) + Content Posting API client.

Docs: developers.tiktok.com/docs/en/login-kit-desktop, content-posting-api-reference-direct-post,
content-posting-api-media-transfer-guide, content-posting-api-reference-get-video-status.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

import requests

from .config import DATA_DIR, load_settings

# Overridable only so the test suite can point the app at a local mock of TikTok.
AUTH_URL = os.environ.get("CLIPPER_TIKTOK_AUTH_URL", "https://www.tiktok.com/v2/auth/authorize/")
API_BASE = os.environ.get("CLIPPER_TIKTOK_API_BASE", "https://open.tiktokapis.com").rstrip("/")
SCOPES = "user.info.basic,video.publish,video.upload"
CALLBACK_PATH = "/api/tiktok/callback/"
ACCOUNT_FILE = DATA_DIR / "tiktok_account.json"

MB = 1024 * 1024
SINGLE_UPLOAD_MAX = 64 * MB  # files up to 64 MB may be sent in one request
CHUNK_SIZE = 32 * MB         # chunks must be 5-64 MB; the last one absorbs the remainder (max 128 MB)

FRIENDLY_ERRORS = {
    "spam_risk_too_many_posts": "TikTok's daily posting limit for this account was reached "
                                "(about 15 posts a day). Try again tomorrow.",
    "spam_risk_user_banned_from_posting": "TikTok says this account can't post right now.",
    "reached_active_user_cap": "Your TikTok developer app reached its daily limit of posting users.",
    "unaudited_client_can_only_post_to_private_accounts":
        "Your TikTok developer app hasn't been audited by TikTok yet, so TikTok only lets it post to a "
        "private account. In the TikTok app go to Settings and privacy → Privacy → turn on Private account, "
        "then retry. (Posts will be 'Only me' until your app passes TikTok's audit.)",
    "privacy_level_option_mismatch": "That 'who can view' option isn't available for this account. "
                                     "Choose another and post again.",
    "access_token_invalid": "Your TikTok connection expired. Reconnect TikTok in the TikTok panel.",
    "scope_not_authorized": "TikTok didn't grant posting permission. Reconnect TikTok and allow all permissions.",
    "rate_limit_exceeded": "TikTok is rate limiting requests. The app will retry shortly.",
    "file_format_check_failed": "TikTok rejected the video format.",
    "duration_check_failed": "The clip is too long or too short for this TikTok account.",
    "frame_rate_check_failed": "TikTok rejected the video's frame rate.",
    "picture_size_check_failed": "TikTok rejected the video's resolution.",
    "internal": "TikTok had a temporary problem. Retry in a few minutes.",
    "auth_removed": "You removed this app's access in TikTok, so the post was stopped.",
    "spam_risk_text": "TikTok flagged the caption text. Edit the caption and try again.",
    "spam_risk": "TikTok flagged this post as risky and blocked it.",
}


class TikTokError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.raw_message = message
        friendly = FRIENDLY_ERRORS.get(code)
        super().__init__(friendly or f"TikTok error: {code}{' — ' + message if message else ''}")


class NotConnected(TikTokError):
    def __init__(self, msg="TikTok isn't connected. Open the TikTok panel and connect your account."):
        RuntimeError.__init__(self, msg)
        self.code = "not_connected"
        self.raw_message = msg


_lock = threading.RLock()
_pending: dict[str, dict] = {}  # OAuth state -> {verifier, redirect_uri, created}


# --------------------------------------------------------------------------- credentials / account

def credentials() -> tuple[str, str]:
    s = load_settings()
    return s.get("tiktok_client_key", "").strip(), s.get("tiktok_client_secret", "").strip()


def configured() -> bool:
    key, secret = credentials()
    return bool(key and secret)


def load_account() -> dict | None:
    with _lock:
        try:
            return json.loads(ACCOUNT_FILE.read_text("utf-8"))
        except (OSError, ValueError):
            return None


def _save_account(acc: dict) -> None:
    with _lock:
        tmp = ACCOUNT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(acc, indent=1), "utf-8")
        tmp.replace(ACCOUNT_FILE)


def public_account() -> dict | None:
    acc = load_account()
    if not acc:
        return None
    return {k: acc.get(k) for k in ("open_id", "display_name", "avatar_url", "scope")} | {
        "can_post": "video.publish" in (acc.get("scope") or ""),
        "can_draft": "video.upload" in (acc.get("scope") or ""),
        "expired": time.time() > acc.get("refresh_expires_at", 0),
    }


# --------------------------------------------------------------------------- OAuth (Login Kit for Desktop)

def authorize_url(redirect_uri: str) -> str:
    key, _ = credentials()
    if not key:
        raise TikTokError("not_configured", "Add your TikTok Client key and secret first.")
    state = secrets.token_urlsafe(24)
    # PKCE: 43-128 unreserved chars; TikTok wants the *hex* SHA-256 of the verifier as the challenge.
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = hashlib.sha256(verifier.encode()).hexdigest()
    with _lock:
        now = time.time()
        for k in [k for k, v in _pending.items() if now - v["created"] > 900]:
            _pending.pop(k, None)
        _pending[state] = {"verifier": verifier, "redirect_uri": redirect_uri, "created": now}
    params = {
        "client_key": key, "response_type": "code", "scope": SCOPES, "redirect_uri": redirect_uri,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _token_request(data: dict) -> dict:
    key, secret = credentials()
    r = requests.post(f"{API_BASE}/v2/oauth/token/", data={"client_key": key, "client_secret": secret, **data},
                      headers={"Cache-Control": "no-cache"}, timeout=30)
    try:
        body = r.json()
    except ValueError:
        raise TikTokError("bad_response", f"HTTP {r.status_code}") from None
    if "access_token" not in body:
        err = body.get("error")
        if isinstance(err, dict):
            raise TikTokError(err.get("code", "oauth_error"), err.get("message", ""))
        raise TikTokError(str(err or "oauth_error"), body.get("error_description", ""))
    return body


def _store_tokens(body: dict, keep: dict | None = None) -> dict:
    now = time.time()
    acc = dict(keep or {})
    acc.update({
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token", acc.get("refresh_token")),
        "open_id": body.get("open_id", acc.get("open_id")),
        "scope": body.get("scope", acc.get("scope", "")),
        "expires_at": now + int(body.get("expires_in", 7200)),
        "refresh_expires_at": now + int(body.get("refresh_expires_in", 180 * 86400)),
    })
    _save_account(acc)
    return acc


def complete_login(state: str, code: str) -> dict:
    with _lock:
        pending = _pending.pop(state, None)
    if not pending:
        raise TikTokError("invalid_state", "The login link expired or was already used. Try connecting again.")
    body = _token_request({
        "code": code, "grant_type": "authorization_code",
        "redirect_uri": pending["redirect_uri"], "code_verifier": pending["verifier"],
    })
    acc = _store_tokens(body)
    try:
        user = _api("GET", "/v2/user/info/?fields=open_id,avatar_url,display_name", acc["access_token"])
        u = user.get("user", {})
        acc.update(display_name=u.get("display_name", ""), avatar_url=u.get("avatar_url", ""))
        _save_account(acc)
    except TikTokError:
        pass
    return acc


def access_token() -> str:
    with _lock:
        acc = load_account()
        if not acc:
            raise NotConnected()
        if time.time() < acc["expires_at"] - 300:
            return acc["access_token"]
        if time.time() > acc.get("refresh_expires_at", 0):
            raise NotConnected("Your TikTok connection expired after 180 days. Reconnect TikTok.")
        try:
            body = _token_request({"grant_type": "refresh_token", "refresh_token": acc["refresh_token"]})
        except TikTokError as e:
            if e.code in ("invalid_grant", "access_token_invalid"):
                raise NotConnected("Your TikTok connection expired. Reconnect TikTok.") from None
            raise
        # Refresh tokens rotate: always keep the newest one.
        return _store_tokens(body, keep=acc)["access_token"]


def disconnect() -> None:
    acc = load_account()
    key, secret = credentials()
    if acc and key and secret:
        try:
            requests.post(f"{API_BASE}/v2/oauth/revoke/",
                          data={"client_key": key, "client_secret": secret, "token": acc["access_token"]},
                          timeout=20)
        except requests.RequestException:
            pass
    with _lock:
        ACCOUNT_FILE.unlink(missing_ok=True)


# --------------------------------------------------------------------------- Content Posting API

def _api(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    try:
        r = requests.request(method, f"{API_BASE}{path}", json=payload, timeout=60, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"})
    except requests.RequestException as e:
        raise TikTokError("network", f"Could not reach TikTok: {e}") from None
    try:
        body = r.json()
    except ValueError:
        raise TikTokError("bad_response", f"HTTP {r.status_code}") from None
    err = body.get("error") or {}
    if err.get("code", "ok") != "ok":
        raise TikTokError(err.get("code", "error"), err.get("message", ""))
    if r.status_code >= 400:
        raise TikTokError("http_error", f"HTTP {r.status_code}")
    return body.get("data") or {}


def creator_info() -> dict:
    return _api("POST", "/v2/post/publish/creator_info/query/", access_token())


def _source_info(size: int) -> dict:
    if size <= SINGLE_UPLOAD_MAX:
        return {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}
    return {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": CHUNK_SIZE,
            "total_chunk_count": size // CHUNK_SIZE}


def _upload(upload_url: str, path: Path, src: dict, on_progress=None) -> None:
    size, chunk, count = src["video_size"], src["chunk_size"], src["total_chunk_count"]
    with path.open("rb") as fh:
        for i in range(count):
            first = i * chunk
            last = size - 1 if i == count - 1 else first + chunk - 1  # final chunk takes the trailing bytes
            fh.seek(first)
            data = fh.read(last - first + 1)
            for attempt in range(4):
                try:
                    r = requests.put(upload_url, data=data, timeout=300, headers={
                        "Content-Type": "video/mp4", "Content-Length": str(len(data)),
                        "Content-Range": f"bytes {first}-{last}/{size}"})
                except requests.RequestException:
                    r = None
                if r is not None and r.status_code in (200, 201, 206):
                    break
                if r is not None and r.status_code < 500:
                    raise TikTokError("upload_failed", f"HTTP {r.status_code} while uploading the video")
                time.sleep(2 * (attempt + 1))
            else:
                raise TikTokError("upload_failed", "TikTok's upload server didn't respond")
            if on_progress:
                on_progress((i + 1) / count)


def direct_post(path: Path, post_info: dict, on_progress=None) -> str:
    src = _source_info(path.stat().st_size)
    data = _api("POST", "/v2/post/publish/video/init/", access_token(),
                {"post_info": post_info, "source_info": src})
    _upload(data["upload_url"], path, src, on_progress)
    return data["publish_id"]


def upload_to_drafts(path: Path, on_progress=None) -> str:
    src = _source_info(path.stat().st_size)
    data = _api("POST", "/v2/post/publish/inbox/video/init/", access_token(), {"source_info": src})
    _upload(data["upload_url"], path, src, on_progress)
    return data["publish_id"]


def fetch_status(publish_id: str) -> dict:
    return _api("POST", "/v2/post/publish/status/fetch/", access_token(), {"publish_id": publish_id})
