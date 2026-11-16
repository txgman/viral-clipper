"""A strict local stand-in for TikTok's Login Kit + Content Posting API, used by the end-to-end test.

It enforces the documented contract (developers.tiktok.com, Nov 2026) so the app can't pass by accident:
PKCE with a hex SHA-256 challenge, exact redirect_uri match, rotating refresh tokens, 6 requests/minute per
token on posting endpoints, chunk rules, Content-Range checks, privacy options from creator_info, and the
"unaudited client" error.

Run: python tests/mock_tiktok.py --port 9555
"""
from __future__ import annotations

import argparse
import hashlib
import re
import secrets
import time
import urllib.parse
from collections import defaultdict, deque

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

CLIENT_KEY, CLIENT_SECRET = "mock_client_key", "mock_client_secret"
MB = 1024 * 1024
app = FastAPI()
state = {
    "codes": {}, "access": {}, "refresh": {}, "uploads": {}, "publish": {}, "log": [],
    "account_private": True, "audited": False, "expires_in": 7200, "posts_today": 0,
}
calls: dict[str, deque] = defaultdict(deque)
CREATOR = {
    "creator_avatar_url": "https://example.invalid/avatar.png", "creator_username": "clipfan",
    "creator_nickname": "Clip Fan", "privacy_level_options": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
    "comment_disabled": False, "duet_disabled": False, "stitch_disabled": True, "max_video_post_duration_sec": 300,
}


def ok(data):
    return JSONResponse({"data": data, "error": {"code": "ok", "message": "", "log_id": "mock"}})


def err(status, code, message=""):
    return JSONResponse({"data": {}, "error": {"code": code, "message": message, "log_id": "mock"}}, status_code=status)


def bearer(request: Request):
    auth = request.headers.get("authorization", "")
    tok = auth.removeprefix("Bearer ").strip()
    info = state["access"].get(tok)
    if not info or info["exp"] < time.time():
        return None, err(401, "access_token_invalid", "The access token is invalid or not found in the request.")
    return info, None


def rate_limited(token: str) -> bool:
    q, now = calls[token], time.time()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= 6:
        return True
    q.append(now)
    return False


@app.get("/v2/auth/authorize/")
def authorize(request: Request):
    q = dict(request.query_params)
    client_key, response_type, scope = q.get("client_key"), q.get("response_type"), q.get("scope", "")
    redirect_uri, st = q.get("redirect_uri", ""), q.get("state", "")
    code_challenge, code_challenge_method = q.get("code_challenge", ""), q.get("code_challenge_method")
    assert client_key == CLIENT_KEY, "bad client_key"
    assert response_type == "code"
    assert code_challenge_method == "S256" and re.fullmatch(r"[0-9a-f]{64}", code_challenge), "challenge must be hex sha256"
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/api/tiktok/callback/", redirect_uri), f"bad redirect {redirect_uri}"
    assert set(scope.split(",")) == {"user.info.basic", "video.publish", "video.upload"}, scope
    code = secrets.token_urlsafe(16)
    state["codes"][code] = {"challenge": code_challenge, "redirect_uri": redirect_uri, "scope": scope}
    return RedirectResponse(redirect_uri + "?" + urllib.parse.urlencode({"code": code, "scopes": scope, "state": st}))


def issue(scope: str, open_id="mock-open-id"):
    at, rt = "act." + secrets.token_urlsafe(16), "rft." + secrets.token_urlsafe(16)
    state["access"][at] = {"exp": time.time() + state["expires_in"], "scope": scope, "open_id": open_id}
    state["refresh"][rt] = {"scope": scope, "open_id": open_id}
    return {"access_token": at, "expires_in": state["expires_in"], "open_id": open_id, "refresh_expires_in": 15552000,
            "refresh_token": rt, "scope": scope, "token_type": "Bearer"}


@app.post("/v2/oauth/token/")
async def token(request: Request):
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    f = dict(urllib.parse.parse_qsl((await request.body()).decode()))
    if f.get("client_key") != CLIENT_KEY or f.get("client_secret") != CLIENT_SECRET:
        return JSONResponse({"error": "invalid_client", "error_description": "bad client"}, status_code=401)
    if f.get("grant_type") == "authorization_code":
        c = state["codes"].pop(f.get("code", ""), None)
        if not c or c["redirect_uri"] != f.get("redirect_uri"):
            return JSONResponse({"error": "invalid_grant", "error_description": "code/redirect mismatch"}, status_code=400)
        if hashlib.sha256(f.get("code_verifier", "").encode()).hexdigest() != c["challenge"]:
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)
        if not 43 <= len(f["code_verifier"]) <= 128:
            return JSONResponse({"error": "invalid_request", "error_description": "verifier length"}, status_code=400)
        state["log"].append("token:code")
        return issue(c["scope"])
    if f.get("grant_type") == "refresh_token":
        r = state["refresh"].pop(f.get("refresh_token", ""), None)  # rotation: old token dies
        if not r:
            return JSONResponse({"error": "invalid_grant", "error_description": "refresh token invalid"}, status_code=400)
        state["log"].append("token:refresh")
        return issue(r["scope"], r["open_id"])
    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


@app.post("/v2/oauth/revoke/")
async def revoke(request: Request):
    f = dict(urllib.parse.parse_qsl((await request.body()).decode()))
    state["access"].pop(f.get("token", ""), None)
    state["log"].append("revoke")
    return {}


@app.get("/v2/user/info/")
def user_info(request: Request, fields: str):
    info, e = bearer(request)
    if e:
        return e
    return ok({"user": {"open_id": info["open_id"], "display_name": "Clip Fan",
                        "avatar_url": "https://example.invalid/avatar.png"}})


@app.post("/v2/post/publish/creator_info/query/")
def creator_info(request: Request):
    info, e = bearer(request)
    if e:
        return e
    if rate_limited(request.headers["authorization"]):
        return err(429, "rate_limit_exceeded")
    state["log"].append("creator_info")
    return ok(CREATOR)


def check_source(src: dict):
    if src.get("source") != "FILE_UPLOAD":
        return "source must be FILE_UPLOAD"
    size, chunk, count = src["video_size"], src["chunk_size"], src["total_chunk_count"]
    if count == 1:
        if chunk != size or size > 64 * MB:
            return "single upload needs chunk_size == video_size <= 64MB"
    else:
        if not 5 * MB <= chunk <= 64 * MB:
            return "chunk_size must be 5-64MB"
        if count != size // chunk or size - (count - 1) * chunk > 128 * MB:
            return "total_chunk_count must be floor(video_size/chunk_size)"
    return None


def start_upload(request: Request, src: dict, kind: str, post_info=None):
    pid = f"v_{kind}_file~v2.{secrets.token_hex(6)}"
    state["uploads"][pid] = {"size": src["video_size"], "chunk": src["chunk_size"], "count": src["total_chunk_count"],
                             "received": 0, "data": bytearray(), "done": False}
    state["publish"][pid] = {"kind": kind, "post_info": post_info, "checks": 0}
    base = str(request.base_url).rstrip("/")
    return ok({"publish_id": pid, "upload_url": f"{base}/upload/?upload_id={pid}&upload_token=t"})


@app.post("/v2/post/publish/video/init/")
async def direct_init(request: Request):
    info, e = bearer(request)
    if e:
        return e
    if "video.publish" not in info["scope"]:
        return err(401, "scope_not_authorized")
    if rate_limited(request.headers["authorization"]):
        return err(429, "rate_limit_exceeded")
    body = await request.json()
    pi, src = body.get("post_info", {}), body.get("source_info", {})
    if not state["audited"] and not state["account_private"]:
        return err(403, "unaudited_client_can_only_post_to_private_accounts")
    if pi.get("privacy_level") not in CREATOR["privacy_level_options"]:
        return err(403, "privacy_level_option_mismatch")
    if not state["audited"] and pi.get("privacy_level") != "SELF_ONLY":
        pass  # real TikTok forces SELF_ONLY visibility for unaudited clients; recorded as-is here
    if len(pi.get("title", "").encode("utf-16-le")) // 2 > 2200:
        return err(400, "invalid_param", "title too long")
    if state["posts_today"] >= 15:
        return err(403, "spam_risk_too_many_posts")
    problem = check_source(src)
    if problem:
        return err(400, "invalid_param", problem)
    state["posts_today"] += 1
    state["log"].append("direct_init")
    return start_upload(request, src, "pub", pi)


@app.post("/v2/post/publish/inbox/video/init/")
async def inbox_init(request: Request):
    info, e = bearer(request)
    if e:
        return e
    if "video.upload" not in info["scope"]:
        return err(401, "scope_not_authorized")
    if rate_limited(request.headers["authorization"]):
        return err(429, "rate_limit_exceeded")
    body = await request.json()
    if set(body) != {"source_info"}:
        return err(400, "invalid_param", "inbox init takes only source_info")
    problem = check_source(body["source_info"])
    if problem:
        return err(400, "invalid_param", problem)
    state["log"].append("inbox_init")
    return start_upload(request, body["source_info"], "inbox")


@app.put("/upload/")
async def upload(request: Request, upload_id: str):
    up = state["uploads"].get(upload_id)
    if not up:
        return Response(status_code=404)
    data = await request.body()
    m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", request.headers.get("content-range", ""))
    if not m or request.headers.get("content-type") not in ("video/mp4", "video/quicktime", "video/webm"):
        return Response(status_code=400)
    first, last, total = map(int, m.groups())
    if int(request.headers.get("content-length", -1)) != len(data) or last - first + 1 != len(data) or total != up["size"]:
        return Response(status_code=400)
    if first != up["received"]:
        return Response(status_code=416)
    up["data"] += data
    up["received"] = last + 1
    if up["received"] == up["size"]:
        up["done"] = True
        return Response(status_code=201, headers={"Content-Range": f"bytes 0-{last}/{total}"})
    return Response(status_code=206, headers={"Content-Range": f"bytes 0-{last}/{total}"})


@app.post("/v2/post/publish/status/fetch/")
async def status(request: Request):
    info, e = bearer(request)
    if e:
        return e
    if rate_limited(request.headers["authorization"]):
        return err(429, "rate_limit_exceeded")
    pid = (await request.json()).get("publish_id")
    p, up = state["publish"].get(pid), state["uploads"].get(pid)
    if not p:
        return err(400, "invalid_param", "unknown publish_id")
    if not up["done"]:
        return ok({"status": "PROCESSING_UPLOAD", "uploaded_bytes": up["received"]})
    p["checks"] += 1
    if p["checks"] < 2:
        return ok({"status": "PROCESSING_UPLOAD", "uploaded_bytes": up["received"]})
    if p["kind"] == "inbox":
        return ok({"status": "SEND_TO_USER_INBOX"})
    return ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": []})


# ---- test controls
@app.post("/mock/config")
async def config(request: Request):
    state.update(await request.json())
    return {k: state[k] for k in ("account_private", "audited", "expires_in", "posts_today")}


@app.get("/mock/state")
def dump():
    return {
        "log": state["log"],
        "posts": [{"publish_id": pid, "kind": p["kind"], "post_info": p["post_info"],
                   "size": state["uploads"][pid]["size"], "received": state["uploads"][pid]["received"],
                   "sha256": hashlib.sha256(state["uploads"][pid]["data"]).hexdigest()}
                  for pid, p in state["publish"].items()],
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9555)
    uvicorn.run(app, host="127.0.0.1", port=ap.parse_args().port, log_level="warning")
