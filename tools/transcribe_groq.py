#!/usr/bin/env python3
"""Transcribe the recordings with Groq's hosted Whisper, for on-screen captions.

Free tier, no install: Groq speaks the OpenAI transcription shape and the
repo already depends on httpx.

    POST https://api.groq.com/openai/v1/audio/transcriptions
    model: whisper-large-v3, response_format: verbose_json
    timestamp_granularities: word + segment

    export GROQ_API_KEY=...          # or leave it in .env
    python tools/transcribe_groq.py              # transcribe everything
    python tools/transcribe_groq.py --rebuild    # re-derive from cache, offline
    python tools/transcribe_groq.py --recorrect  # reapply FIXUPS only, offline

Why this exists alongside tools/transcribe_recordings.py
--------------------------------------------------------
That one runs faster-whisper "base" locally and feeds tools/cut_segments.py,
which only needs word timings good enough to find a phrase boundary. This
one runs large-v3, whose text is accurate enough to *show a caller*. Segment
cutting keeps using the local file; captions use this one. Neither depends
on the other, and the local path still works with no key and no network.

What it transcribes
-------------------
The four full recordings and the four 20s card demos — eight requests. The
sixteen chat segments are exact slices of the full recordings, so their
captions are derived here by offsetting, not re-sent. Re-transcribing a cut
would also risk the caption disagreeing with the clip it sits under.

Every response is cached under assets/segments/_groq_raw/. Deriving caption
lines from a transcript involves real judgement — where to break a line, how
to put the punctuation back, how to handle Whisper's jitter — and getting
that wrong should cost a `--rebuild`, not eight more API calls.

Writes assets/segments/captions.json:

    {"kwame": [{"start": 0.0, "end": 3.6, "text": "...",
                "words": [["Hello,", 0.0, 0.28], ...]}, ...],
     ...,
     "tracks": {"assets/kwame_card_demo.mp3": {"agent": "kwame",
                "duration": 20.0, "lines": [...]}, ...}}

The agent keys hold the full recording's lines. `tracks` maps every audio
file the landing page can play to the captions for that file, which is what
the on-page display actually reads.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import pathlib
import re
import sys
import time
from typing import Optional

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
ASSETS = ROOT / "assets"
SEGDIR = ASSETS / "segments"
MANIFEST = SEGDIR / "manifest.json"
RAW = SEGDIR / "_groq_raw"
OUT = SEGDIR / "captions.json"

sys.path.insert(0, str(HERE))
from cut_segments import FIXUPS  # noqa: E402  (same proper-noun corrections)

ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL = "whisper-large-v3"
TIMEOUT_S = 120.0
RETRIES = 3

RECORDINGS = {
    "kwame": "kwame_real_voice.mp3",
    "amina": "amina_real_voice.mp3",
    "kofi": "kofi_real_voice.mp3",
    "maya": "maya_real_voice.mp3",
}

# The short demo on each agent card. A separate cut, so a separate request:
# its timings do not line up with the full recording.
CARD_DEMOS = {name: f"{name}_card_demo.mp3" for name in RECORDINGS}

# A caption line longer than this is hard to read at a glance and overflows
# the panel it sits in, so it gets split on the nearest word boundary. Sized
# for the narrowest surface that shows one: the hero phone renders about
# 196px wide, which holds roughly this many characters on two rendered rows.
MAX_LINE_CHARS = 42

# Shortest span a word may occupy. Whisper sometimes reports start == end,
# which would make a word impossible to highlight.
MIN_WORD_S = 0.04

# Whisper takes a prompt as a style hint. Naming the brands gets them right
# at the source rather than in FIXUPS afterwards, and a punctuated example
# discourages the long unpunctuated runs it otherwise emits when it loses
# the sentence structure. It is a hint, not a constraint — FIXUPS stays.
PROMPT = (
    "A customer service phone call. Akwaaba. Adinkra Hotel, Asaase Restaurant, "
    "Cynea Bank, Cynea Scheduling. Asante sana. Medase. Jollof rice, kelewele, "
    "banku, tilapia, cedis, shillings, mobile money. "
    "Hello, this is Kwame. How can I help you today? Your booking is confirmed."
)


# ── input ────────────────────────────────────────────────────────────────

def read_key() -> str:
    """GROQ_API_KEY from the environment, falling back to .env."""
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        return key
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("GROQ_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def correct(text: str) -> str:
    """Apply the same proper-noun corrections the segment cutter uses.

    large-v3 gets these right more often than base did, but "more often"
    is not "always", and a caption is read, not skimmed.
    """
    for wrong, right in FIXUPS:
        text = re.sub(wrong, right, text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def fetch(client, path: pathlib.Path, key: str, refresh: bool) -> dict:
    """One file through Groq, cached on disk. Returns the verbose_json payload."""
    cached = RAW / f"{path.stem}.json"
    if cached.exists() and not refresh:
        print(f"  cached {path.name}")
        return json.loads(cached.read_text(encoding="utf-8"))

    for attempt in range(1, RETRIES + 1):
        with path.open("rb") as fh:
            response = client.post(
                ENDPOINT,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (path.name, fh, "audio/mpeg")},
                data={
                    "model": MODEL,
                    "response_format": "verbose_json",
                    "language": "en",
                    "prompt": PROMPT,
                    # A list value repeats the field, which is how the
                    # OpenAI-shaped API takes more than one granularity.
                    "timestamp_granularities[]": ["word", "segment"],
                },
                timeout=TIMEOUT_S,
            )
        if response.status_code == 200:
            payload = response.json()
            RAW.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(payload, indent=1, ensure_ascii=False),
                              encoding="utf-8")
            return payload
        # 429 is the free tier's rate limit and 5xx is transient; both are
        # worth waiting out. Anything else is a real error, so say so now.
        if response.status_code in (429, 500, 502, 503) and attempt < RETRIES:
            wait = float(response.headers.get("retry-after", 2 * attempt))
            print(f"    {response.status_code}, retrying in {wait:.0f}s "
                  f"({attempt}/{RETRIES - 1})", flush=True)
            time.sleep(wait)
            continue
        raise SystemExit(f"Groq returned {response.status_code} for {path.name}: "
                         f"{response.text[:300]}")
    raise SystemExit(f"Groq did not answer for {path.name} after {RETRIES} tries")


# ── transcript -> caption lines ──────────────────────────────────────────

def expand(words: list[dict]) -> list[list]:
    """Whisper's word list, one token per entry.

    Groq sometimes packs several tokens into a single entry — "is $120." is
    one "word" spanning two seconds. Left packed, the index-alignment below
    slips by however many tokens were hidden, and a caption loses its last
    few words. Splitting the span proportionally by token length keeps the
    one-token-one-timing invariant everything downstream assumes.
    """
    out: list[list] = []
    for entry in words:
        tokens = str(entry.get("word", "")).split()
        if not tokens:
            continue
        start, end = float(entry["start"]), float(entry["end"])
        if len(tokens) == 1:
            out.append([tokens[0], start, end])
            continue
        total = sum(len(t) for t in tokens) or len(tokens)
        at = start
        for token in tokens:
            share = (end - start) * len(token) / total
            out.append([token, at, at + share])
            at += share
    return out


def timed_tokens(seg: dict, words: list[list]) -> list[list]:
    """The segment's punctuated words, each with a start and end.

    Whisper reports two views of the same speech: `segments[].text` is
    punctuated and capitalised, `words[]` carries the timings but is
    stripped of punctuation. Captions need both, so the two are zipped by
    index. When the counts disagree — Whisper occasionally merges "it's"
    one way in the text and another in the word list — each token borrows
    the timing of the proportionally nearest word, which keeps the
    punctuation and costs a few tens of milliseconds of precision.
    """
    start, end = float(seg["start"]), float(seg["end"])
    mine = [w for w in words if start <= (w[1] + w[2]) / 2 <= end]
    tokens = correct(seg.get("text", "")).split()

    if not tokens:
        return []

    if not mine:                                    # timings missing: spread evenly
        span = max(end - start, MIN_WORD_S * len(tokens))
        step = span / len(tokens)
        return [[t, round(start + i * step, 2), round(start + (i + 1) * step, 2)]
                for i, t in enumerate(tokens)]

    if len(tokens) == len(mine):
        pairs = zip(tokens, mine)
    else:
        last = len(mine) - 1
        span = max(len(tokens) - 1, 1)
        pairs = ((t, mine[round(i * last / span)]) for i, t in enumerate(tokens))

    return [[t, round(w[1], 2), round(w[2], 2)] for t, w in pairs]


def split_long(line: dict) -> list[dict]:
    """Break an over-long line at a word boundary, keeping punctuation.

    Whisper's own segmentation sometimes runs three sentences together. A
    caption needing two rendered lines is fine; one needing four is a wall
    of text moving at speech speed. Breaks prefer the end of a sentence.
    """
    if len(line["text"]) <= MAX_LINE_CHARS or len(line["words"]) < 2:
        return [line]

    out, current = [], []
    for word in line["words"]:
        current.append(word)
        text = " ".join(w[0] for w in current)
        ends_sentence = word[0].endswith((".", "?", "!"))
        if len(text) >= MAX_LINE_CHARS or (ends_sentence and len(text) > MAX_LINE_CHARS // 2):
            out.append({"start": current[0][1], "end": current[-1][2],
                        "text": text, "words": current})
            current = []
    if current:
        out.append({"start": current[0][1], "end": current[-1][2],
                    "text": " ".join(w[0] for w in current), "words": current})
    return out


def settle(lines: list[dict], duration: float) -> list[dict]:
    """Make the timeline safe to drive a highlight from.

    Whisper's word times jitter: a word can start fractionally before the
    one preceding it, and the last word can end after the file does. Either
    makes a caption jump backwards or point past the audio, so starts are
    forced non-decreasing, every word gets a non-zero span, and nothing is
    allowed past `duration`.
    """
    limit = round(duration, 2) if duration > 0 else None
    previous = 0.0

    for line in lines:
        for word in line["words"]:
            start = max(float(word[1]), previous)
            end = max(float(word[2]), start + MIN_WORD_S)
            if limit is not None:
                start = min(start, limit)
                end = min(end, limit)
            word[1], word[2] = round(start, 2), round(end, 2)
            previous = word[1]
        if line["words"]:
            line["start"] = line["words"][0][1]
            line["end"] = max(w[2] for w in line["words"])

    return [l for l in lines if l["words"] and l["text"].strip()]


def to_lines(payload: dict) -> list[dict]:
    """Whisper's verbose_json -> caption lines with per-word timings."""
    words = expand(payload.get("words", []))
    duration = float(payload.get("duration", 0) or 0)
    segments = payload.get("segments") or []

    if not segments:                                   # no segmentation: one line
        text = correct(payload.get("text", ""))
        if not text:
            return []
        segments = [{"start": 0.0, "end": duration, "text": text}]

    lines: list[dict] = []
    for seg in segments:
        tokens = timed_tokens(seg, words)
        if not tokens:
            continue
        lines.extend(split_long({"start": tokens[0][1], "end": tokens[-1][2],
                                 "text": " ".join(t[0] for t in tokens),
                                 "words": tokens}))

    lines.sort(key=lambda l: l["start"])
    return settle(lines, duration)


def key(token: str) -> str:
    return "".join(c for c in token.lower() if c.isalnum())


def align(tokens: list[str], words: list[list],
          start: float, end: float) -> list[list]:
    """Give a known list of words timings borrowed from the transcript.

    The sixteen chat segments were cut using the *local* model's word
    timings, but captions come from Groq's. The two disagree by up to half a
    second on where a word begins, so clipping Groq's timeline to the local
    model's cut points drops a leading word here and adds a trailing one
    there — a caption that does not match the clip playing under it.

    So the segment's own text wins: it is exactly what the clip says, having
    been cut on that basis. Only the timings are borrowed, matched token for
    token against the transcript and interpolated across whatever does not
    match. Time drift then moves a highlight a few frames; it can no longer
    change the words.
    """
    if not tokens:
        return []

    pad = 1.5                                   # generous: drift is sub-second
    nearby = [w for w in words if w[2] > start - pad and w[1] < end + pad]

    spans: list[Optional[tuple[float, float]]] = [None] * len(tokens)
    if nearby:
        matcher = difflib.SequenceMatcher(a=[key(t) for t in tokens],
                                          b=[key(w[0]) for w in nearby],
                                          autojunk=False)
        for i, j, size in matcher.get_matching_blocks():
            for k in range(size):
                spans[i + k] = (nearby[j + k][1], nearby[j + k][2])

    known = [i for i, s in enumerate(spans) if s is not None]
    if not known:                               # nothing matched: spread evenly
        step = (end - start) / len(tokens)
        spans = [(start + i * step, start + (i + 1) * step) for i in range(len(tokens))]
    else:
        # Interpolate the unmatched tokens between their matched neighbours,
        # and extrapolate at the edges at the local average word rate.
        rate = ((spans[known[-1]][1] - spans[known[0]][0]) / max(len(known), 1)) or 0.3
        for i in range(len(spans)):
            if spans[i] is not None:
                continue
            before = [k for k in known if k < i]
            after = [k for k in known if k > i]
            if before and after:
                lo, hi = before[-1], after[0]
                at = spans[lo][1]
                step = (spans[hi][0] - spans[lo][1]) / (hi - lo)
                spans[i] = (at + step * (i - lo - 1), at + step * (i - lo))
            elif before:
                spans[i] = (spans[before[-1]][1] + rate * (i - before[-1] - 1),
                            spans[before[-1]][1] + rate * (i - before[-1]))
            else:
                first = spans[after[0]][0]
                spans[i] = (first - rate * (after[0] - i),
                            first - rate * (after[0] - i - 1))

    out = []
    for token, (s, e) in zip(tokens, spans):
        out.append([token, round(s - start, 2), round(e - start, 2)])
    return out


def segment_lines(text: str, words: list[list], start: float, end: float) -> list[dict]:
    """Caption lines for one chat segment, rebased so the clip begins at 0."""
    timed = align(text.split(), words, start, end)
    if not timed:
        return []
    lines = split_long({"start": timed[0][1], "end": timed[-1][2],
                        "text": " ".join(t[0] for t in timed), "words": timed})
    return settle(lines, round(end - start, 2))


# ── offline modes ────────────────────────────────────────────────────────

def build_from_cache(agents: list[str]) -> tuple[dict, dict]:
    """Derive captions and tracks from whatever is cached, no network."""
    captions: dict = {}
    tracks: dict = {}

    for agent in agents:
        for kind, filename in (("full", RECORDINGS[agent]), ("card", CARD_DEMOS[agent])):
            cached = RAW / f"{pathlib.Path(filename).stem}.json"
            if not cached.exists():
                print(f"  no cached transcript for {filename}", file=sys.stderr)
                continue
            payload = json.loads(cached.read_text(encoding="utf-8"))
            lines = to_lines(payload)
            duration = round(float(payload.get("duration", 0) or 0), 2)
            tracks[f"assets/{filename}"] = {"agent": agent, "duration": duration,
                                            "lines": lines}
            if kind == "full":
                captions[agent] = lines
            print(f"  {filename}: {len(lines)} lines, "
                  f"{sum(len(l['words']) for l in lines)} words")
    return captions, tracks


def derive_segments(captions: dict, tracks: dict) -> int:
    """Add the sixteen chat-segment tracks by slicing the full recordings."""
    if not MANIFEST.exists():
        print(f"{MANIFEST} not found — no segment captions derived", file=sys.stderr)
        return 0

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    derived = 0
    for agent, entry in manifest["agents"].items():
        if agent not in captions:
            continue
        words = [w for line in captions[agent] for w in line["words"]]
        for seg in entry["segments"].values():
            lines = segment_lines(seg["text"], words, seg["start"], seg["end"])
            if lines:
                tracks[seg["file"]] = {"agent": agent, "duration": seg["duration"],
                                       "lines": lines}
                derived += 1
    return derived


def write(captions: dict, tracks: dict) -> None:
    out = dict(captions)
    out["tracks"] = dict(sorted(tracks.items()))
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} — {len(tracks)} tracks, "
          f"{OUT.stat().st_size / 1024:.0f} KB")


def load_existing() -> tuple[dict, dict]:
    """Whatever a previous run produced, so --only tops up rather than empties."""
    if not OUT.exists():
        return {}, {}
    existing = json.loads(OUT.read_text(encoding="utf-8"))
    return existing, existing.pop("tracks", {})


def recorrect() -> int:
    """Reapply FIXUPS to an existing captions.json, without re-transcribing.

    Whisper finds a new way to spell a proper noun roughly every time the
    model changes. Adding a row to FIXUPS should not cost eight more API
    calls, so this rewrites the text in place; timings are untouched.
    """
    if not OUT.exists():
        print(f"{OUT} does not exist yet — run without --recorrect first.",
              file=sys.stderr)
        return 1

    data = json.loads(OUT.read_text(encoding="utf-8"))
    changed = 0

    def fix(lines: list[dict]) -> None:
        nonlocal changed
        for line in lines:
            before = line["text"]
            line["text"] = correct(before)
            if line["text"] != before:
                changed += 1
            line["words"] = [[correct(w[0]), w[1], w[2]] for w in line["words"]]

    for key, value in data.items():
        if key == "tracks":
            for track in value.values():
                fix(track["lines"])
        else:
            fix(value)

    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"recorrected {changed} caption lines in {OUT.relative_to(ROOT)}")
    return 0


# ── entry point ──────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="one agent only (kwame|amina|kofi|maya)")
    ap.add_argument("--rebuild", action="store_true",
                    help="re-derive captions.json from the cached transcripts; no network")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore the cache and re-transcribe")
    ap.add_argument("--recorrect", action="store_true",
                    help="reapply the FIXUPS corrections only; no network")
    args = ap.parse_args()

    if args.recorrect:
        return recorrect()

    agents = [args.only] if args.only else list(RECORDINGS)
    for agent in agents:
        if agent not in RECORDINGS:
            print(f"unknown agent {agent!r}; have {list(RECORDINGS)}", file=sys.stderr)
            return 1

    if args.rebuild:
        captions, tracks = load_existing()
        fresh_captions, fresh_tracks = build_from_cache(agents)
        captions.update(fresh_captions)
        tracks.update(fresh_tracks)
    else:
        try:
            import httpx
        except ImportError:
            print("httpx is not installed:  pip install httpx", file=sys.stderr)
            return 1

        key = read_key()
        if not key:
            print("GROQ_API_KEY is not set (checked the environment and .env).\n"
                  "To work from previously cached transcripts instead:\n"
                  "    python tools/transcribe_groq.py --rebuild", file=sys.stderr)
            return 1

        captions, tracks = load_existing()
        with httpx.Client() as client:
            for agent in agents:
                for kind, filename in (("full", RECORDINGS[agent]),
                                       ("card", CARD_DEMOS[agent])):
                    path = ASSETS / filename
                    if not path.exists():
                        print(f"  skipping missing {filename}", file=sys.stderr)
                        continue
                    print(f"transcribing {filename} ...", flush=True)
                    payload = fetch(client, path, key, args.refresh)
                    lines = to_lines(payload)
                    duration = round(float(payload.get("duration", 0) or 0), 2)
                    tracks[f"assets/{filename}"] = {"agent": agent, "duration": duration,
                                                    "lines": lines}
                    if kind == "full":
                        captions[agent] = lines
                    print(f"  {len(lines)} lines, "
                          f"{sum(len(l['words']) for l in lines)} words, {duration:.1f}s")

    derived = derive_segments(captions, tracks)
    print(f"derived captions for {derived} chat segments from the full recordings")
    write(captions, tracks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
