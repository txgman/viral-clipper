"""Finds the most viral moments.

Primary: Claude reads the timestamped transcript and picks clips.
Fallback (no API key / API error): a built-in scorer using vocal energy, hook words and pacing.
"""
from __future__ import annotations

import contextlib
import json
import math
import re
from collections import Counter

import numpy as np

HOP = 0.5  # seconds per loudness sample


# --------------------------------------------------------------------------- sentences

def join_words(words: list[dict]) -> str:
    out = ""
    for w in words:
        t = w["w"]
        out += t if (not out or re.match(r"^[^\w$#@(]", t) or "-" == out[-1:]) else " " + t
    return out.strip()


def build_sentences(segments: list[dict]) -> list[dict]:
    words = [w for seg in segments for w in seg["words"]]
    sentences, cur = [], []

    def close():
        if cur:
            sentences.append({
                "s": cur[0]["s"], "e": cur[-1]["e"],
                "text": join_words(cur),
                "words": list(cur),
            })
            cur.clear()

    for w in words:
        if cur and (w["s"] - cur[-1]["e"] > 0.9 or w["e"] - cur[0]["s"] > 14):
            close()
        cur.append(w)
        if re.search(r"[.?!…][\"')\]]*$", w["w"]) and w["e"] - cur[0]["s"] >= 1.0:
            close()
    close()
    for i, s in enumerate(sentences):
        s["i"] = i
    return sentences


def _energy(curve: np.ndarray, s: float, e: float) -> float:
    if len(curve) == 0:
        return -30.0
    a = max(0, int(s / HOP))
    b = min(len(curve), max(a + 1, int(math.ceil(e / HOP))))
    return float(np.mean(curve[a:b])) if b > a else float(curve[min(a, len(curve) - 1)])


def _clip_bounds(sents: list[dict], i: int, j: int, duration: float) -> tuple[float, float]:
    start = sents[i]["s"] - 0.2
    if i > 0:
        start = max(start, sents[i - 1]["e"] + 0.02)
    end = sents[j]["e"] + 0.5
    if j + 1 < len(sents):
        end = min(end, max(sents[j]["e"] + 0.08, sents[j + 1]["s"] - 0.05))
    return round(max(0.0, start), 3), round(min(duration, end), 3)


def _fit_length(sents: list[dict], i: int, j: int, lo: float, hi: float) -> tuple[int, int]:
    n = len(sents)
    i, j = max(0, min(i, n - 1)), max(0, min(j, n - 1))
    if j < i:
        i, j = j, i

    def span(a, b):
        return sents[b]["e"] - sents[a]["s"]

    while span(i, j) > hi and j > i:
        j -= 1
    while span(i, j) < lo:
        if j + 1 < n and span(i, j + 1) <= hi:
            j += 1
        elif i > 0 and span(i - 1, j) <= hi:
            i -= 1
        else:
            break
    return i, j


def _non_overlapping(clips: list[dict], count: int) -> list[dict]:
    chosen: list[dict] = []
    for c in sorted(clips, key=lambda c: -c["score"]):
        if all(c["end"] <= o["start"] + 1.0 or c["start"] >= o["end"] - 1.0 for o in chosen):
            chosen.append(c)
        if len(chosen) >= count:
            break
    return chosen


# --------------------------------------------------------------------------- heuristic

STOP = set("""a an the and or but if then so of to in on at by for with from as is are was were be been being
i me my we our you your he she it they them his her its their this that these those there here what which who
whom whose when where why how not no yes do does did done have has had just like really very can could would
should will shall may might must am im i'm it's that's don't dont gonna wanna yeah okay ok oh um uh know think
going get got go say said one all some any more most much many also too out up down about into over than now
well right thing things kind sort lot way even back because us let let's ever before after never always every
time times year years today make made take took want went come came look looked people person something someone
nothing anything everything maybe still only through where while again ago other another same such those""".split())

HOOK_WORDS = set("""you your never always secret nobody everyone everybody why how stop don't biggest worst best
mistake mistakes truth actually crazy insane money million millions billion dollars free first last wrong hate love
shocking fact facts imagine story died dead killed fired quit rich broke rule rules lie lied lying problem
important seriously literally honestly unbelievable impossible warning danger dangerous huge massive changed
learned regret afraid scared""".split())

LAUGH = re.compile(r"\b(ha(ha)+|lol|laugh|laughing|\[laughter\])\b", re.I)


def _hook_score(text: str) -> float:
    words = re.findall(r"[a-z0-9']+", text.lower())[:14]
    score = sum(1.0 for w in words if w in HOOK_WORDS) * 0.35
    score += 0.6 if "?" in text else 0.0
    score += 0.4 if "!" in text else 0.0
    score += 0.4 if re.search(r"\d", text) else 0.0
    return min(score, 2.0)


def _keywords(text: str, k: int = 3) -> list[str]:
    words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z']{3,}", text.lower()) if w not in STOP]
    return [w for w, _ in Counter(words).most_common(k)]


FILLER = re.compile(
    r"^(?:so|and|but|okay|ok|well|anyway|um+|uh+|like|yeah|yes|now|right|alright|look|listen|see|i mean|you know|"
    r"actually|basically|honestly|oh)\b[,.!]?\s+", re.I)


def tidy_title(text: str, n: int = 60) -> str:
    """Clean a title: drop filler openers, quotes and trailing periods; cut long ones at a natural pause."""
    t = re.sub(r"\s+", " ", text or "").strip().strip("\"'“”‘’ ")
    for _ in range(3):
        t2 = FILLER.sub("", t)
        if t2 == t or len(t2) < 12:
            break
        t = t2
    t = re.sub(r"[.,;:…]+$", "", t).strip()
    if len(t) > n:
        cut = max((m.start() for m in re.finditer(r"[,;:—–]| - ", t[: n + 1])), default=-1)
        if cut >= 24:
            t = t[:cut].rstrip()
        else:
            t = t[: n - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return t[:1].upper() + t[1:] if t else t


def heuristic_title(clip_sents: list[dict], energy: list[float] | None = None) -> str:
    """Pick the clip's punchiest line: hook words, questions, short and emphatic beats early lines that are flat."""
    def score(k, s):
        words = len(s["text"].split())
        e = energy[k] if energy is not None and k < len(energy) else 0.0
        return (_hook_score(s["text"]) + (0.5 if s["text"].rstrip().endswith("?") else 0)
                + (0.4 if 4 <= words <= 12 else 0.15 if words <= 18 else 0) + 0.35 * e - 0.08 * k)
    cands = [(k, s) for k, s in enumerate(clip_sents) if len(s["text"].split()) >= 3] or list(enumerate(clip_sents[:1]))
    best = max(cands, key=lambda ks: score(*ks))[1]
    return tidy_title(best["text"])


def _shorten(text: str, n: int = 70) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def heuristic_clips(sents: list[dict], curve: np.ndarray, duration: float,
                    count: int, lo: float, hi: float) -> list[dict]:
    if not sents:
        return _energy_only_clips(curve, duration, count, lo, hi)

    energies = np.array([_energy(curve, s["s"], s["e"]) for s in sents])
    mu, sd = float(np.mean(energies)), float(np.std(energies) or 1.0)
    ez = (energies - mu) / sd
    rates = np.array([len(s["words"]) / max(s["e"] - s["s"], 0.3) for s in sents])
    rz = (rates - rates.mean()) / (rates.std() or 1.0)

    windows = []
    n = len(sents)
    for i in range(n):
        j = i
        while j < n and sents[j]["e"] - sents[i]["s"] <= hi:
            dur = sents[j]["e"] - sents[i]["s"]
            if dur >= lo:
                idx = range(i, j + 1)
                w = np.array([sents[k]["e"] - sents[k]["s"] for k in idx])
                text = " ".join(sents[k]["text"] for k in idx)
                kw = sum(1 for t in re.findall(r"[a-z']+", text.lower()) if t in HOOK_WORDS)
                score = (
                    1.0 * float(np.average(ez[i:j + 1], weights=w))
                    + 0.5 * float(np.max(ez[i:j + 1]))
                    + 1.2 * _hook_score(sents[i]["text"])
                    + 0.3 * float(np.mean(rz[i:j + 1]))
                    + 0.8 * min(kw / dur * 10, 2.0)
                    + 0.6 * (1.0 if LAUGH.search(text) else 0.0)
                    + 0.3 * (1.0 if re.search(r"[.?!]$", sents[j]["text"]) else 0.0)
                )
                windows.append((score, i, j))
            j += 1
    if not windows:  # video shorter than the minimum clip length
        windows = [(0.0, *_fit_length(sents, 0, n - 1, lo, hi))]

    all_scores = np.array([w[0] for w in windows])
    candidates = []
    for score, i, j in windows:
        start, end = _clip_bounds(sents, i, j, duration)
        pct = float((all_scores < score).mean())
        text = " ".join(sents[k]["text"] for k in range(i, j + 1))
        candidates.append({
            "start": start, "end": end, "score": int(round(50 + 45 * pct)),
            "title": heuristic_title(sents[i:j + 1], list(ez[i:j + 1])),
            "hook": sents[i]["text"],
            "reason": "High vocal energy and a strong opening line (built-in scoring).",
            "caption": _shorten(text, 150),
            "hashtags": ["#" + k for k in _keywords(text)] + ["#shorts", "#viral"],
        })
    return _non_overlapping(candidates, count)


def _energy_only_clips(curve: np.ndarray, duration: float, count: int, lo: float, hi: float) -> list[dict]:
    length = min((lo + hi) / 2, duration)
    if len(curve) == 0 or duration <= length:
        return [{"start": 0.0, "end": round(min(duration, hi), 3), "score": 50, "title": "Highlight",
                 "hook": "", "reason": "No speech detected; whole video used.", "caption": "",
                 "hashtags": ["#shorts"]}]
    mu, sd = float(curve.mean()), float(curve.std() or 1.0)
    z = (curve - mu) / sd
    cands = []
    t = 0.0
    while t + length <= duration:
        a, b = int(t / HOP), int((t + length) / HOP)
        seg = z[a:b]
        cands.append({"start": round(t, 3), "end": round(t + length, 3),
                      "score_raw": float(seg.mean() + 0.5 * seg.std())})
        t += 1.0
    raw = np.array([c["score_raw"] for c in cands])
    for c in cands:
        c["score"] = int(round(50 + 45 * float((raw < c.pop("score_raw")).mean())))
        c.update({"title": "Highlight", "hook": "", "reason": "Loudest, most energetic stretch (no speech detected).",
                  "caption": "", "hashtags": ["#shorts", "#viral"]})
    return _non_overlapping(cands, count)


# --------------------------------------------------------------------------- Claude

SYSTEM_PROMPT = """You are an expert short-form video editor who has cut thousands of viral clips for TikTok, \
YouTube Shorts and Instagram Reels. You receive the timestamped transcript of a long video and pick the moments \
most likely to go viral as standalone vertical clips.

What makes a clip go viral:
- A scroll-stopping hook in the first 1-3 seconds: a bold claim, a surprising fact, a question, conflict, or strong \
emotion. The first line must make sense to someone who has not seen the rest of the video.
- It is self-contained: no unexplained references to earlier parts of the video.
- A payoff: a punchline, reveal, lesson, strong opinion, or story resolution. End right after the payoff, on a \
complete sentence.
- Emotion and energy: laughter, anger, shock, vulnerability, excitement. Each line has a loud: value from 0 (quiet) \
to 9 (loud) describing vocal energy; spikes often mark the best moments.
- Shareable value: a quotable line, a contrarian take, practical advice, a relatable moment.

Rules:
- Each clip is a contiguous range of transcript lines, given as start_line and end_line (inclusive), using the L \
numbers.
- Clip duration (end time of end_line minus start time of start_line) must be within the requested range.
- Clips must not overlap.
- Order clips best first. viral_score is 0-100; be honest and reserve 90+ for exceptional moments.
- title: the clip's headline, used as the YouTube Shorts title and on-screen hook. 3-10 words, at most 60 \
characters. Lead with the most intriguing specific detail (a number, a bold claim, a surprising outcome, a sharp \
question) and open a curiosity gap without giving away the payoff. Write it the way a creator would: sentence case, \
plain words, no quotes, emojis, hashtags or trailing period. It must be accurate to what is said: no misleading \
clickbait. Style examples: "The interview question that got me hired", "Why your savings never grow", \
"I quit my job over one email". Each clip's title must be distinct.
- hook: the clip's opening line, as spoken.
- reason: one or two sentences on why this moment will perform.
- caption: a TikTok-ready caption in the creator's voice: one hook-first line that creates curiosity without giving away the payoff, optionally followed by a short question that invites comments. At most 150 characters, no hashtags in this field.
- hashtags: 3-5 specific, relevant hashtags, each starting with #. Mix one broader topic tag with niche ones; avoid generic spam tags like #fyp, #viral or #foryou.
- Write title, caption and hashtags in the language the video is spoken in."""

CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "viral_score": {"type": "integer"},
                    "reason": {"type": "string"},
                    "caption": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["start_line", "end_line", "title", "hook", "viral_score", "reason",
                             "caption", "hashtags"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}

MAX_CHARS_PER_REQUEST = 600_000  # ~150k tokens of transcript per request


class AIError(RuntimeError):
    pass


def _fmt(t: float) -> str:
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:04.1f}" if t >= 3600 else \
        f"{int(t // 60):02d}:{t % 60:04.1f}"


def _lines(sents: list[dict], curve: np.ndarray) -> list[str]:
    energies = np.array([_energy(curve, s["s"], s["e"]) for s in sents])
    ranks = energies.argsort().argsort() / max(len(energies) - 1, 1)
    return [f"L{s['i']} [{_fmt(s['s'])}-{_fmt(s['e'])}] (loud:{int(round(r * 9))}) {s['text']}"
            for s, r in zip(sents, ranks)]


def _call_claude(client, model: str, user_text: str, system: str = SYSTEM_PROMPT, schema: dict = CLIP_SCHEMA,
                 effort: str = "high", key: str = "clips", max_tokens: int = 16000) -> list[dict]:
    import anthropic

    common = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_text}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    try:
        # Server-side fallback re-runs the request on another model if this one declines.
        response = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **common)
    except anthropic.BadRequestError:
        response = client.messages.create(**common)

    if response.stop_reason == "refusal":
        raise AIError("Claude declined to analyze this video.")
    if response.stop_reason == "max_tokens":
        raise AIError("Claude's answer was cut off (max_tokens).")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise AIError("Claude returned no answer.")
    return json.loads(text)[key]


@contextlib.contextmanager
def _api_errors():
    import anthropic

    try:
        yield
    except anthropic.AuthenticationError:
        raise AIError("The Anthropic API key was rejected. Check it in Settings.") from None
    except anthropic.PermissionDeniedError:
        raise AIError("The Anthropic API key does not have access to this model.") from None
    except anthropic.RateLimitError:
        raise AIError("Anthropic rate limit reached. Try again in a minute.") from None
    except anthropic.APIConnectionError:
        raise AIError("Could not reach the Anthropic API (network problem).") from None
    except anthropic.APIStatusError as e:
        raise AIError(f"Anthropic API error ({e.status_code}): {e.message}") from None
    except (ValueError, KeyError) as e:
        raise AIError(f"Could not read Claude's answer: {e}") from None


def claude_clips(sents: list[dict], curve: np.ndarray, duration: float, count: int, lo: float, hi: float,
                 video_title: str, description: str, api_key: str, model: str) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=600)
    lines = _lines(sents, curve)

    chunks, cur, size = [], [], 0
    for line in lines:
        if cur and size + len(line) > MAX_CHARS_PER_REQUEST:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append(cur)

    raw: list[dict] = []
    with _api_errors():
        for idx, chunk in enumerate(chunks):
            want = count if len(chunks) == 1 else max(2, math.ceil(count * len(chunk) / len(lines)) + 1)
            part = f" (part {idx + 1} of {len(chunks)} of a long video)" if len(chunks) > 1 else ""
            user_text = (
                f"Video title: {video_title}\n"
                + (f"Video description: {description[:600]}\n" if description else "")
                + f"Video length: {_fmt(duration)}\n\n"
                f"Find the {want} most viral moments{part}. Each clip must be {lo:.0f}-{hi:.0f} seconds long.\n\n"
                "<transcript>\n" + "\n".join(chunk) + "\n</transcript>"
            )
            raw.extend(_call_claude(client, model, user_text))

    clips = []
    for c in raw:
        try:
            i, j = _fit_length(sents, int(c["start_line"]), int(c["end_line"]), lo, hi)
        except (TypeError, ValueError):
            continue
        start, end = _clip_bounds(sents, i, j, duration)
        tags = [t if t.startswith("#") else "#" + t for t in (c.get("hashtags") or []) if t.strip()]
        clips.append({
            "start": start, "end": end,
            "score": int(max(0, min(100, int(c.get("viral_score", 50))))),
            "title": tidy_title(c.get("title") or "", 70) or heuristic_title(sents[i:j + 1]),
            "hook": c.get("hook") or sents[i]["text"],
            "reason": c.get("reason", ""),
            "caption": c.get("caption", ""),
            "hashtags": [t.replace(" ", "") for t in tags][:8],
        })
    if not clips:
        raise AIError("Claude did not return any usable clips.")
    return _non_overlapping(clips, count)


# --------------------------------------------------------------------------- smart TikTok captions

CAPTION_SYSTEM = """You write captions for short vertical videos posted to TikTok by the video's own creator. Captions should make viewers stop, watch to the end, and comment.

Guidelines:
- Lead with a hook: a curiosity gap, a bold claim from the clip, or a relatable feeling. Don't spoil the payoff.
- Sound like a real person, not a brand. Short sentences. At most one emoji, and only if it fits.
- Optionally end with a short question that invites comments.
- Keep the caption text (without hashtags) under 150 characters.
- 3-5 hashtags: specific to the topic and audience, plus at most one broader topic tag. Never use generic spam tags like #fyp, #foryou, #viral or #trending.
- Be accurate to what is said in the clip. No misleading clickbait.
- Write in the same language as the clip transcript.
- Each option should take a different angle (e.g. curiosity, bold statement, relatable/question)."""

CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "caption": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["caption", "hashtags"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["options"],
    "additionalProperties": False,
}


def tiktok_captions(clip: dict, video_title: str, api_key: str, model: str) -> list[dict]:
    """Three alternative TikTok captions (caption text + hashtags) for one clip."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=180)
    user_text = (
        f"Source video title: {video_title}\n"
        f"Clip title: {clip.get('title', '')}\n"
        f"Why this clip should perform: {clip.get('reason', '')}\n"
        f"Clip length: {clip.get('duration', 0):.0f} seconds\n\n"
        f"<clip_transcript>\n{clip.get('transcript', '')}\n</clip_transcript>\n\n"
        "Write 3 caption options for posting this clip on TikTok."
    )
    with _api_errors():
        options = _call_claude(client, model, user_text, system=CAPTION_SYSTEM, schema=CAPTION_SCHEMA,
                               effort="medium", key="options", max_tokens=8000)
    out = []
    for o in options:
        tags = [t.strip().replace(" ", "") for t in o.get("hashtags", []) if t.strip()]
        tags = [t if t.startswith("#") else "#" + t for t in tags][:6]
        if o.get("caption", "").strip():
            out.append({"caption": o["caption"].strip(), "hashtags": tags})
    if not out:
        raise AIError("Claude did not return any captions.")
    return out[:3]
