"""Cynea — keyword responder over pre-cut human voice segments.

Zero cost and zero network. Every reply is a slice of a recording a real
person already made, selected by keyword. No TTS, no LLM, no API key.

    from cynea.audio_responder import respond

    r = respond("what are your rates?", "kwame")
    r.intent        # 'pricing'
    r.file          # 'assets/segments/kwame/pricing.mp3'
    r.duration      # 8.46
    r.text          # "That's $200 per night. Breakfast is included..."

**What this is and is not.** It is a soundboard: it can only say the
phrases that were recorded, and it picks between them on keywords, not
comprehension. Ask it something outside its vocabulary and it falls back
rather than answering — `Response.confident` is False whenever that
happens, so a caller-facing surface can escalate instead of bluffing. It
is not a substitute for the real engine in cynea/engine.py, and anything
showing it to the public should label it as a scripted demo.

If you need arbitrary sentences for free, the repo already depends on
edge-tts (free, 9 voices, 4 African accents) — see cynea/providers.py.
This module exists for the case where a genuine human voice matters more
than saying anything you like.

The segment map lives in assets/segments/manifest.json, produced by
tools/cut_segments.py. Edit the plan there and re-run it; nothing in this
module hardcodes a phrase.
"""

from __future__ import annotations

import functools
import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Optional

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "assets" / "segments" / "manifest.json"


class ManifestMissing(RuntimeError):
    """Raised when the segment manifest has not been generated yet."""


@dataclass(frozen=True)
class Response:
    """One selected segment, ready to play."""

    agent: str
    intent: str
    file: str                       # repo-relative path to the mp3
    duration: float                 # seconds
    text: str                       # what the segment actually says
    confident: bool = True          # False when nothing matched and we fell back
    matched: Optional[str] = None   # the trigger phrase that fired
    alternatives: tuple = field(default=())  # other intents that also matched

    @property
    def path(self) -> pathlib.Path:
        """Absolute path to the audio file."""
        return ROOT / self.file

    def __str__(self) -> str:
        tag = self.intent if self.confident else f"{self.intent} (fallback)"
        return f"[{self.agent}/{tag}] {self.text}"


@functools.lru_cache(maxsize=1)
def load_manifest() -> dict:
    if not MANIFEST.exists():
        raise ManifestMissing(
            f"{MANIFEST} not found. Generate it with:\n"
            "    python tools/transcribe_recordings.py\n"
            "    python tools/cut_segments.py"
        )
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def agents() -> list[str]:
    """Agent names that have segments."""
    return sorted(load_manifest()["agents"])


def segments_for(agent: str) -> dict[str, dict]:
    """The intent -> segment map for one agent."""
    entry = load_manifest()["agents"].get(agent.lower())
    if entry is None:
        raise KeyError(f"unknown agent {agent!r}; have {agents()}")
    return entry["segments"]


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation to spaces, collapse whitespace.

    Padded with spaces so a trigger can be tested with plain substring
    containment and still only match on word boundaries: " pay " will not
    fire inside "repayment".
    """
    return " " + re.sub(r"[^a-z0-9]+", " ", text.lower()).strip() + " "


def detect_intents(user_text: str, agent: str) -> list[tuple[str, str]]:
    """Every intent the utterance triggers, best first.

    Ranked by the length of the matched trigger, so "how much" beats a bare
    "much" and a two-word phrase beats an incidental single word. Only
    intents this agent can actually voice are considered — asking Amina for
    a room rate matches nothing, which is the correct answer.
    """
    manifest = load_manifest()
    available = segments_for(agent)
    haystack = _normalise(user_text)

    hits: list[tuple[int, str, str]] = []
    for intent, triggers in manifest["triggers"].items():
        if intent not in available:
            continue
        best = None
        for trigger in triggers:
            needle = _normalise(trigger).rstrip() + " "
            if needle in haystack:
                if best is None or len(trigger) > len(best):
                    best = trigger
        if best:
            hits.append((len(best), intent, best))

    hits.sort(key=lambda h: (-h[0], h[1]))
    return [(intent, trigger) for _, intent, trigger in hits]


def respond(user_text: str, agent_name: str) -> Response:
    """Pick the segment that answers `user_text`, for `agent_name`.

    Always returns a Response. When nothing matches, it falls back to the
    agent's fallback segment with `confident=False` rather than raising or
    returning None, so a caller never has dead air to handle.
    """
    agent = agent_name.lower().strip()
    available = segments_for(agent)
    hits = detect_intents(user_text, agent)

    if hits:
        intent, trigger = hits[0]
        seg = available[intent]
        return Response(
            agent=agent, intent=intent, file=seg["file"],
            duration=seg["duration"], text=seg["text"],
            confident=True, matched=trigger,
            alternatives=tuple(i for i, _ in hits[1:]),
        )

    fallback = load_manifest()["agents"][agent].get("fallback", "greeting")
    if fallback not in available:                 # last resort: anything at all
        fallback = next(iter(available))
    seg = available[fallback]
    return Response(
        agent=agent, intent=fallback, file=seg["file"],
        duration=seg["duration"], text=seg["text"],
        confident=False, matched=None,
    )
