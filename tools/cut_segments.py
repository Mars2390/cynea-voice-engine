#!/usr/bin/env python3
"""Cut the four real-voice recordings into intent segments with ffmpeg.

Free: ffmpeg only, no service calls. Reads the word-level timings written by
tools/transcribe_recordings.py so each segment starts and ends on a word
boundary rather than an arbitrary offset — otherwise "pricing.mp3" would
just be a slice of tape that happens to sit near a price.

    python tools/transcribe_recordings.py    # once, writes transcripts.json
    python tools/cut_segments.py             # writes assets/segments/**

Each segment is defined by the first and last few words of the phrase we
want. Whisper mishears proper nouns (Adinkra -> "Odin Krohotel", Cynea ->
"Sinia", cedis -> "CDs"), so anchors deliberately use ordinary words that
transcribe reliably. Boundaries are padded slightly and faded at both ends
so a segment never opens or closes on a clipped consonant.

Output: assets/segments/<agent>/<intent>.mp3 plus a manifest.json that the
responder and the landing page both read.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SEGDIR = ASSETS / "segments"
TRANSCRIPTS = SEGDIR / "transcripts.json"
MANIFEST = SEGDIR / "manifest.json"

LEAD_IN = 0.18      # seconds of air before the first word
TAIL = 0.30         # seconds of air after the last word
FADE = 0.12         # fade in/out, so no segment clicks

# (intent, start anchor, end anchor). Anchors match on a run of words,
# case- and punctuation-insensitive.
PLAN: dict[str, list[tuple[str, str, str]]] = {
    "kwame": [
        ("greeting", "hello yes",                 "help you today"),
        ("pricing",  "that's 200 per night",      "deluxe is 120"),
        ("booking",  "your booking is confirmed", "confirmation email shortly"),
        ("closing",  "thank you for calling",     "rest of your day"),
    ],
    "amina": [
        ("greeting", "hello this is amina",       "help you today"),
        ("balance",  "your current balance",      "a payment of 2000 shillings"),
        ("transfer", "would you like me to help", "with that transfer"),
        ("closing",  "thank you for calling",     "have a good day"),
    ],
    "kofi": [
        ("greeting", "aquaba this is kofi",        "take your order"),
        ("order",    "your order number is",      "ready in 25 minutes"),
        ("delivery", "delivery to that area",     "is that okay"),
        ("closing",  "thank you for calling",     "come again soon"),
    ],
    "maya": [
        ("greeting", "hello this is maya",        "help you today"),
        ("scheduling", "let me check availability", "at 10 in the morning"),
        ("confirmation", "your appointment is confirmed", "an sms reminder"),
        ("closing",  "thank you for choosing",    "see you on tuesday"),
    ],
}

# What the caller can say to trigger each intent. Shared triggers first, then
# the per-agent ones. Matched as whole words against the lowercased utterance.
TRIGGERS: dict[str, list[str]] = {
    "greeting":     ["hello", "hi", "hey", "good morning", "good afternoon",
                     "good evening", "hallo", "habari", "sannu"],
    "pricing":      ["price", "prices", "cost", "costs", "rate", "rates",
                     "how much", "charge", "fee", "expensive", "cheap"],
    "booking":      ["book", "booking", "reserve", "reservation", "room",
                     "stay", "night", "nights"],
    "balance":      ["balance", "account", "how much do i have", "statement",
                     "money", "funds"],
    "transfer":     ["transfer", "send money", "pay", "payment", "deposit",
                     "withdraw"],
    "order":        ["order", "menu", "food", "eat", "jollof", "rice",
                     "chicken", "hungry"],
    "delivery":     ["deliver", "delivery", "how long", "when will it arrive",
                     "takeaway", "take away", "pickup"],
    "scheduling":   ["schedule", "appointment", "availability", "available",
                     "slot", "when can i", "free"],
    "confirmation": ["confirm", "confirmed", "confirmation", "is it booked",
                     "did it work"],
    "closing":      ["thank", "thanks", "bye", "goodbye", "that's all",
                     "nothing else", "cheers", "asante", "medase"],
}


# Whisper mishears the proper nouns in these recordings. The anchors above
# route around that by matching on ordinary words, but the transcript text
# is shown to the public on the landing page, so it gets corrected here.
# Left side is what Whisper wrote, right side what the speaker actually said.
FIXUPS: list[tuple[str, str]] = [
    (r"\bOdin Krohotel\b",     "Adinkra Hotel"),
    (r"\ba ?dinkro hotel\b",   "Adinkra Hotel"),
    (r"\ba Sassy Restaurant\b", "Asaase Restaurant"),
    (r"\b(?:Sassy|Asasi|Asase|Asaasi|Asaasa)\b", "Asaase"),
    (r"\bA ?Kwaba\b",          "Akwaaba"),
    (r"\b(?:Aquaba|Akwaba|Aqwaba)\b", "Akwaaba"),
    # Run the compound before the bare name: Whisper writes the bank as one
    # word ("Sineabank"), which no \b-anchored rule for "Cynea" can reach.
    (r"\b(?:Sin[ie]a|C[iy]n[ie]a|Synea) ?bank\b", "Cynea Bank"),
    (r"\b(?:Sinia|Sinea|Cinea|Cinia|Synea|Cynia|Sineya)\b", "Cynea"),
]

# Whisper emits a split number as two tokens ("15" + ",000"), and joining
# word tokens on spaces would render that as "15 ,000".
_SPACE_BEFORE_PUNCT = re.compile(r"\s+(?=[,.;:!?%])")


def clean_text(words: list[dict], first: int, last: int) -> str:
    """The words of one segment, as a sentence a reader should see."""
    text = _SPACE_BEFORE_PUNCT.sub("", " ".join(w["w"] for w in words[first:last + 1]))
    for wrong, right in FIXUPS:
        text = re.sub(wrong, right, text, flags=re.IGNORECASE)
    return text


def norm(word: str) -> str:
    return "".join(c for c in word.lower() if c.isalnum())


def find(words: list[dict], anchor: str, start_at: int = 0) -> tuple[int, int] | None:
    """Locate a phrase. Returns (first_index, last_index) or None.

    Matching runs over a concatenated stream of normalised characters rather
    than word tokens, because Whisper splits numbers across tokens — "2,000"
    arrives as "2" + ",000". Token-wise comparison misses those; a character
    stream with an index map back to words does not.
    """
    stream, owner = [], []
    for i in range(start_at, len(words)):
        for ch in norm(words[i]["w"]):
            stream.append(ch)
            owner.append(i)
    hay = "".join(stream)
    needle = "".join(norm(w) for w in anchor.split())
    if not needle:
        return None
    at = hay.find(needle)
    if at < 0:
        return None
    return owner[at], owner[at + len(needle) - 1]


def cut(src: pathlib.Path, dst: pathlib.Path, start: float, end: float) -> None:
    dur = round(end - start, 3)
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
         "-i", str(src),
         "-af", f"afade=t=in:st=0:d={FADE},afade=t=out:st={max(0, dur - FADE):.3f}:d={FADE}",
         "-c:a", "libmp3lame", "-b:a", "128k", "-ac", "1", "-ar", "44100", str(dst)],
        check=True,
    )


def main() -> int:
    if not shutil.which("ffmpeg"):
        print("ffmpeg is not on PATH.", file=sys.stderr)
        return 1
    if not TRANSCRIPTS.exists():
        print(f"missing {TRANSCRIPTS}. Run tools/transcribe_recordings.py first.",
              file=sys.stderr)
        return 1

    data = json.loads(TRANSCRIPTS.read_text(encoding="utf-8"))
    manifest: dict[str, dict] = {"agents": {}, "triggers": TRIGGERS}
    problems: list[str] = []

    for agent, plan in PLAN.items():
        rec = data[agent]
        words = rec["words"]
        src = ASSETS / rec["file"]
        segments: dict[str, dict] = {}

        for intent, a_start, a_end in plan:
            hit_s = find(words, a_start)
            if not hit_s:
                problems.append(f"{agent}/{intent}: start anchor not found: {a_start!r}")
                continue
            hit_e = find(words, a_end, start_at=hit_s[0])
            if not hit_e:
                problems.append(f"{agent}/{intent}: end anchor not found: {a_end!r}")
                continue

            start = max(0.0, words[hit_s[0]]["start"] - LEAD_IN)
            end = min(rec["duration"], words[hit_e[1]]["end"] + TAIL)
            if end <= start:
                problems.append(f"{agent}/{intent}: end before start")
                continue

            rel = f"assets/segments/{agent}/{intent}.mp3"
            cut(src, ROOT / rel, start, end)
            segments[intent] = {
                "file": rel,
                "start": round(start, 2),
                "end": round(end, 2),
                "duration": round(end - start, 2),
                "text": clean_text(words, hit_s[0], hit_e[1]),
            }
            print(f"  {agent}/{intent:<13} {start:6.2f}-{end:6.2f}  "
                  f"{end - start:5.2f}s  {segments[intent]['text'][:58]}")

        manifest["agents"][agent] = {
            "source": rec["file"],
            "segments": segments,
            "fallback": "greeting",
        }

    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"\nwrote {MANIFEST.relative_to(ROOT)}")

    if problems:
        print("\nunresolved anchors:", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
