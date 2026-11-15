"""Renders a vertical 1080x1920 clip: face-tracking crop (or blurred-background fit),
word-by-word burned-in captions, H.264/AAC MP4 at -14 LUFS — ready for TikTok, Reels and Shorts."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .config import CASCADE_DIR, FONT_PATH

OUT_W, OUT_H, FPS = 1080, 1920, 30
SAMPLE_FPS = 6
CAPTION_STYLES = {"yellow": (255, 229, 0), "green": (61, 255, 110), "white": (255, 255, 255)}


class RenderError(RuntimeError):
    pass


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def work_size(w: int, h: int) -> tuple[int, int]:
    """Decode resolution: cap the short side so 4K sources don't slow everything down."""
    if min(w, h) > 1080:
        s = 1080 / min(w, h)
        return _even(w * s), _even(h * s)
    return _even(w), _even(h)


# --------------------------------------------------------------------------- framing analysis

def _cascades():
    base = CASCADE_DIR
    front = cv2.CascadeClassifier(str(base / "haarcascade_frontalface_alt2.xml"))
    profile = cv2.CascadeClassifier(str(base / "haarcascade_profileface.xml"))
    if front.empty():
        raise RenderError(f"Face detection model missing from {base}.")
    return front, profile


def _detect(gray, front, profile, min_px):
    faces = list(front.detectMultiScale(gray, 1.1, 5, minSize=(min_px, min_px)))
    if not faces and not profile.empty():
        faces = list(profile.detectMultiScale(gray, 1.1, 6, minSize=(min_px, min_px)))
        if not faces:
            flipped = cv2.flip(gray, 1)
            w = gray.shape[1]
            faces = [(w - x - fw, y, fw, fh) for (x, y, fw, fh) in
                     profile.detectMultiScale(flipped, 1.1, 6, minSize=(min_px, min_px))]
    return faces


def analyze_framing(src: Path, start: float, dur: float, fw: int, fh: int,
                    cancelled: Callable[[], bool]) -> dict:
    sh = 480
    sw = _even(fw * sh / fh)
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
         "-vf", f"fps={SAMPLE_FPS},scale={sw}:{sh}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    front, profile = _cascades()
    crop_frac = (fh * 9 / 16) / fw
    size = sw * sh * 3
    xs, cuts, face_hits, multi_hits = [], [], 0, 0
    prev_hist, prev_x = None, None
    try:
        while True:
            if cancelled():
                raise RenderError("Cancelled")
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            img = np.frombuffer(buf, np.uint8).reshape(sh, sw, 3)
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
            cv2.normalize(hist, hist)
            cut = prev_hist is not None and cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL) < 0.55
            prev_hist = hist
            if cut:
                prev_x = None
            cuts.append(cut)

            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = _detect(gray, front, profile, int(sh * 0.06))
            if faces:
                face_hits += 1
                cands = [((x + w / 2) / sw, w / sw) for (x, y, w, h) in faces]
                big = max(c[1] for c in cands)
                main = [c for c in cands if c[1] >= 0.6 * big]
                if len(main) >= 2 and max(c[0] for c in main) - min(c[0] for c in main) > crop_frac * 0.8:
                    multi_hits += 1

                def pref(c):
                    stay = 1.0 if prev_x is None else 1 - 0.6 * min(1.0, abs(c[0] - prev_x) / 0.3)
                    return c[1] * stay
                prev_x = max(cands, key=pref)[0]
                xs.append(prev_x)
            else:
                xs.append(np.nan)
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()

    n = len(xs)
    if n == 0:
        return {"face_ratio": 0.0, "multi_ratio": 0.0, "path": np.array([0.5]), "shot": np.array([0])}

    shot = np.cumsum(np.array(cuts, dtype=int))
    xs = np.array(xs, dtype=float)
    path = np.full(n, 0.5)
    for sid in np.unique(shot):
        idx = np.where(shot == sid)[0]
        vals = xs[idx]
        if np.mean(~np.isnan(vals)) < 0.3:
            path[idx] = np.nan  # no (reliable) face in this shot
            continue
        # hold the last known face position through missed detections
        filled = vals.copy()
        last = vals[~np.isnan(vals)][0]
        for k in range(len(filled)):
            if np.isnan(filled[k]):
                filled[k] = last
            else:
                last = filled[k]
        if len(filled) >= 5:
            pad = np.pad(filled, 2, mode="edge")
            filled = np.array([np.median(pad[k:k + 5]) for k in range(len(filled))])
        if filled.max() - filled.min() < 0.08:
            filled[:] = np.median(filled)  # static shot: lock the frame like a camera operator would
        else:  # zero-phase smoothing so the virtual camera pans smoothly
            a = 0.25
            fwd = filled.copy()
            for k in range(1, len(fwd)):
                fwd[k] = a * filled[k] + (1 - a) * fwd[k - 1]
            bwd = fwd.copy()
            for k in range(len(bwd) - 2, -1, -1):
                bwd[k] = a * fwd[k] + (1 - a) * bwd[k + 1]
            filled = bwd
        path[idx] = filled
    return {"face_ratio": face_hits / n, "multi_ratio": multi_hits / max(face_hits, 1),
            "path": path, "shot": shot}


def _x_at(framing: dict, t: float) -> float:
    path, shot = framing["path"], framing["shot"]
    k = t * SAMPLE_FPS
    a = min(int(k), len(path) - 1)
    b = min(a + 1, len(path) - 1)
    if shot[a] != shot[b] or np.isnan(path[a]) or np.isnan(path[b]):
        return float(path[a] if k - a < 0.5 else path[b])
    f = min(max(k - a, 0.0), 1.0)
    return float(path[a] * (1 - f) + path[b] * f)


# --------------------------------------------------------------------------- captions

def _clean(word: str) -> str:
    word = re.sub(r"[^\w'?!%$&@#\-]", "", word, flags=re.UNICODE).strip("-")
    return word.upper()


class Captions:
    def __init__(self, words: list[dict], style: str):
        self.color = CAPTION_STYLES.get(style, CAPTION_STYLES["yellow"])
        self.font = ImageFont.truetype(str(FONT_PATH), 88)
        self.cache: dict[tuple[int, int], tuple | None] = {}
        pages, cur = [], []
        for w in words:
            text = _clean(w["w"])
            if not text:
                continue
            if cur and not re.search(r"\w", text[0]):  # "%", "'S" etc. belong to the previous word
                cur[-1]["t"] += text
                cur[-1]["e"] = w["e"]
                continue
            chars = sum(len(x["t"]) + 1 for x in cur)
            if cur and (len(cur) >= 3 or chars + len(text) > 17 or w["s"] - cur[-1]["e"] > 0.6):
                pages.append(cur)
                cur = []
            cur.append({"t": text, "s": w["s"], "e": w["e"]})
            if re.search(r"[.?!,;:]$", w["w"]):
                pages.append(cur)
                cur = []
        if cur:
            pages.append(cur)
        self.pages = pages
        self.spans = []
        for i, p in enumerate(pages):
            end = p[-1]["e"] + 0.6
            if i + 1 < len(pages):
                end = min(end, pages[i + 1][0]["s"])
            self.spans.append((p[0]["s"] - 0.05, max(end, p[-1]["e"])))

    def _render(self, pi: int, active: int):
        page = self.pages[pi]
        font, space = self.font, self.font.getlength(" ")
        widths = [font.getlength(w["t"]) for w in page]
        lines, cur, cur_w = [], [], 0.0
        for k, w in enumerate(widths):
            add = w + (space if cur else 0)
            if cur and cur_w + add > 940:
                lines.append(cur)
                cur, cur_w = [], 0.0
                add = w
            cur.append(k)
            cur_w += add
        if cur:
            lines.append(cur)
        line_h = 112
        img = Image.new("RGBA", (OUT_W, line_h * len(lines) + 40), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        for li, line in enumerate(lines):
            total = sum(widths[k] for k in line) + space * (len(line) - 1)
            x = (OUT_W - total) / 2
            y = 20 + li * line_h
            for k in line:
                fill = self.color if k == active else (255, 255, 255)
                draw.text((x + 3, y + 6), page[k]["t"], font=font, fill=(0, 0, 0, 150),
                          stroke_width=9, stroke_fill=(0, 0, 0, 150))
                draw.text((x, y), page[k]["t"], font=font, fill=fill + (255,),
                          stroke_width=9, stroke_fill=(0, 0, 0, 255))
                x += widths[k] + space
        arr = np.array(img)
        alpha = arr[..., 3]
        rows, cols = np.where(alpha.any(axis=1))[0], np.where(alpha.any(axis=0))[0]
        if not len(rows):
            return None
        arr = arr[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
        bgr = arr[..., 2::-1].astype(np.uint16)
        a = arr[..., 3:4].astype(np.uint16)
        y0 = int(OUT_H * 0.70) - arr.shape[0] // 2
        return y0, int(cols[0]), bgr * a, 255 - a

    def draw(self, frame: np.ndarray, t: float) -> None:
        for pi, (s, e) in enumerate(self.spans):
            if s <= t < e:
                page = self.pages[pi]
                active = max((k for k, w in enumerate(page) if w["s"] <= t + 0.03), default=0)
                key = (pi, active)
                if key not in self.cache:
                    self.cache[key] = self._render(pi, active)
                ov = self.cache[key]
                if ov is None:
                    return
                y0, x0, pre, inv = ov
                h, w = pre.shape[:2]
                region = frame[y0:y0 + h, x0:x0 + w]
                region[:] = ((region.astype(np.uint16) * inv + pre) // 255).astype(np.uint8)
                return


# --------------------------------------------------------------------------- compositing

def _compose_crop(frame, cx, crop_w):
    fh, fw = frame.shape[:2]
    x0 = int(round(cx * fw - crop_w / 2))
    x0 = max(0, min(fw - crop_w, x0))
    return cv2.resize(frame[:, x0:x0 + crop_w], (OUT_W, OUT_H), interpolation=cv2.INTER_CUBIC)


def _compose_fit(frame):
    fh, fw = frame.shape[:2]
    # blurred, darkened background filling the whole 9:16 canvas
    if fw / fh > 9 / 16:
        cw, ch = int(fh * 9 / 16), fh
    else:
        cw, ch = fw, int(fw * 16 / 9)
    x0, y0 = (fw - cw) // 2, (fh - ch) // 2
    small = cv2.resize(frame[y0:y0 + ch, x0:x0 + cw], (OUT_W // 8, OUT_H // 8), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), 6)
    out = cv2.resize(small, (OUT_W, OUT_H), interpolation=cv2.INTER_LINEAR)
    out = (out.astype(np.uint16) * 140 // 255).astype(np.uint8)
    # sharp full frame in the middle
    s = min(OUT_W / fw, OUT_H / fh)
    nw, nh = min(OUT_W, _even(fw * s)), min(OUT_H, _even(fh * s))
    fg = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    px, py = (OUT_W - nw) // 2, (OUT_H - nh) // 2
    out[py:py + nh, px:px + nw] = fg
    return out


def choose_layout(mode: str, framing: dict, fw: int, fh: int) -> str:
    if fw / fh <= 9 / 16 + 0.02:
        return "fit"  # already vertical
    if mode in ("fit", "center"):
        return mode
    if mode == "track":
        return "track"
    return "track" if framing["face_ratio"] >= 0.2 and framing["multi_ratio"] < 0.5 else "fit"


def _measure_lufs(src: Path, start: float, dur: float, chain: str = "anull") -> float | None:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
         "-vn", "-af", f"{chain},ebur128", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    found = re.findall(r"I:\s+(-?[\d.]+|-inf) LUFS", proc.stderr)
    try:
        return float(found[-1])
    except (IndexError, ValueError):
        return None


def _loudness_filter(src: Path, start: float, dur: float, target: float = -14.0) -> str:
    """Normalize to -14 LUFS (the level TikTok, YouTube and Instagram play at) with a -1.5 dBTP
    peak limiter, like a mastering engineer would. Second pass corrects for what the limiter eats."""
    limiter = "alimiter=limit=0.84:attack=5:release=50:level=false"
    level = _measure_lufs(src, start, dur)
    if level is None or level < -60:  # silence or unmeasurable
        return "anull"
    gain = min(target - level, 24.0)
    for _ in range(3):
        measured = _measure_lufs(src, start, dur, f"volume={gain:.2f}dB,{limiter}")
        if measured is None or measured < -60 or abs(target - measured) < 0.3:
            break
        gain = min(gain + (target - measured), 24.0)
    return f"volume={gain:.2f}dB,{limiter}"


def render_clip(src: Path, out: Path, start: float, end: float, words: list[dict], probe: dict,
                layout_mode: str, caption_style: str, log_dir: Path,
                on_progress: Callable[[float], None], cancelled: Callable[[], bool]) -> dict:
    dur = end - start
    fw, fh = work_size(probe["width"], probe["height"])

    framing = analyze_framing(src, start, dur, fw, fh, cancelled)
    layout = choose_layout(layout_mode, framing, fw, fh)
    crop_w = min(_even(fh * 9 / 16), fw)
    captions = None
    if caption_style != "none":
        rel = [{"w": w["w"], "s": w["s"] - start, "e": w["e"] - start}
               for w in words if w["e"] > start and w["s"] < end]
        if rel:
            captions = Captions(rel, caption_style)

    dec_log = open(log_dir / f"{out.stem}.decode.log", "wb")
    enc_log = open(log_dir / f"{out.stem}.encode.log", "wb")
    dec = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
         "-vf", f"fps={FPS},scale={fw}:{fh}:flags=bicubic,setsar=1",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, stderr=dec_log,
    )
    enc_cmd = ["ffmpeg", "-y", "-v", "error",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OUT_W}x{OUT_H}", "-r", str(FPS), "-i", "-"]
    if probe["has_audio"]:
        enc_cmd += ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src)]
        audio_filter = ["-af", _loudness_filter(src, start, dur)]
    else:
        enc_cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        audio_filter = []
    enc_cmd += ["-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high",
                "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", *audio_filter,
                "-movflags", "+faststart", "-shortest", str(out)]
    enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stderr=enc_log)

    frame_bytes = fw * fh * 3
    total = max(1, int(dur * FPS))
    used_layouts: dict[str, int] = {}
    k = 0
    try:
        while True:
            if cancelled():
                raise RenderError("Cancelled")
            buf = dec.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            frame = np.frombuffer(buf, np.uint8).reshape(fh, fw, 3)
            t = k / FPS
            if layout == "fit" or crop_w >= fw:
                img, used = _compose_fit(frame), "fit"
            elif layout == "center":
                img, used = _compose_crop(frame, 0.5, crop_w), "center"
            else:
                x = _x_at(framing, t)
                if np.isnan(x):  # shot without a face: show the whole frame (or center crop if forced)
                    img, used = (_compose_fit(frame), "fit") if layout_mode == "auto" else \
                        (_compose_crop(frame, 0.5, crop_w), "track")
                else:
                    img, used = _compose_crop(frame, x, crop_w), "track"
            used_layouts[used] = used_layouts.get(used, 0) + 1
            if captions:
                captions.draw(img, t)
            enc.stdin.write(img.tobytes())
            k += 1
            if k % 15 == 0:
                on_progress(min(k / total, 1.0))
    except BrokenPipeError:
        pass
    except BaseException:
        dec.kill()
        enc.kill()
        raise
    finally:
        dec.stdout.close()
        dec.wait()
        try:
            enc.stdin.close()
        except BrokenPipeError:
            pass
        enc.wait()
        dec_log.close()
        enc_log.close()

    if enc.returncode != 0 or k == 0 or not out.exists():
        err = (log_dir / f"{out.stem}.encode.log").read_text(errors="replace")[-500:]
        derr = (log_dir / f"{out.stem}.decode.log").read_text(errors="replace")[-300:]
        raise RenderError(f"Rendering failed. {err or derr or 'No frames decoded.'}")
    on_progress(1.0)
    main = [name for name, n in used_layouts.items() if n >= 0.15 * k]
    return {"layout": main[0] if len(main) == 1 else "mixed", "faces": round(framing["face_ratio"], 2), "frames": k}
