#!/usr/bin/env python3
"""Check the stitched conversations against the audio they claim to describe.

    python tools/check_conversations.py

Runs offline against assets/segments/conversations.json and the two mp3s.
Everything it asserts is measured from the waveform, not from a second
opinion about it — see "what this does not check" at the bottom.

Checks
------
1. Structure. Turns are in order, never overlap, alternate between the two
   speakers, and every word sits inside the turn that owns it with its
   onsets in ascending order.
2. Gaps. Every pause between turns is between 200ms and 400ms.
3. Boundaries. The gaps are digital silence, so they are visible in the
   waveform. Each planned boundary has to line up with one that is really
   there. This is what proves the caption timeline and the audio are the
   same timeline: if a clip had come out a different length than planned,
   every boundary after it would have slipped by the difference.
4. Duration. The file is as long as the plan says it is.

What this does not check, and why
---------------------------------
Where each *word* falls inside its turn. Those onsets come from Groq's
word timings on the source take, shifted by an exact per-turn offset.
Verifying them would mean transcribing the stitched file and comparing, and
two Whisper passes over the same speech disagree with each other by up to a
second — a re-transcription of these two files put the worst disagreement at
1.07s while the boundaries below were provably good to 0.14s. A test whose
noise is seven times its tolerance is not a test. Word-level sync is checked
where it is visible instead: tools/check_captions.py drives real Chromium
and asserts the words on screen at time t are the ones due by t.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONV = ROOT / "assets/segments/conversations.json"

GAP_MIN, GAP_MAX = 0.19, 0.41      # the 200-400ms the brief asks for
BOUNDARY_TOL = 0.20                # a fade ramp reads as silence a little early
DURATION_TOL = 0.35


def deep_silences(path: pathlib.Path) -> list[tuple[float, float]]:
    """Stretches quiet enough to be the inserted gaps rather than a pause
    for breath. -50dB is well under anything a microphone picks up and well
    over true digital zero."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "silencedetect=noise=-50dB:d=0.15", "-f", "null", "-"],
        capture_output=True, text=True).stderr
    spans, start = [], None
    for m in re.finditer(r"silence_(start|end):\s*(-?[\d.]+)", out):
        if m.group(1) == "start":
            start = float(m.group(2))
        elif start is not None:
            spans.append((start, float(m.group(2))))
            start = None
    return spans


def duration(path: pathlib.Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    return float(out.stdout.strip())


def main() -> int:
    if not CONV.exists():
        print(f"{CONV.relative_to(ROOT)} is missing — run tools/build_conversations.py")
        return 1
    conv = json.loads(CONV.read_text(encoding="utf-8"))
    bad: list[str] = []

    for name, cv in conv.items():
        audio = ROOT / cv["audio"]
        if not audio.exists():
            bad.append(f"{name}: {cv['audio']} is missing")
            continue
        lines = cv["lines"]
        print(f"{name}: {len(lines)} turns, {cv['duration']:.2f}s")

        # 1. structure
        for i, L in enumerate(lines):
            if L["end"] <= L["start"]:
                bad.append(f"{name} turn {i}: end is not after start")
            ws = [w[1] for w in L["words"]]
            if ws != sorted(ws):
                bad.append(f"{name} turn {i}: word onsets are not ascending")
            if ws and (ws[0] < L["start"] - 0.01 or ws[-1] > L["end"] + 0.01):
                bad.append(f"{name} turn {i}: a word falls outside its own turn")
            if not L["text"].strip():
                bad.append(f"{name} turn {i}: empty text")
            if i and lines[i]["speaker"] == lines[i - 1]["speaker"]:
                bad.append(f"{name} turn {i}: two turns in a row from "
                           f"{L['speaker']} — the call does not alternate")
            if i and lines[i]["start"] < lines[i - 1]["end"] - 0.001:
                bad.append(f"{name} turn {i}: overlaps the turn before it")

        # 2. gaps
        gaps = [lines[i + 1]["start"] - lines[i]["end"] for i in range(len(lines) - 1)]
        if gaps:
            lo, hi = min(gaps), max(gaps)
            print(f"  gaps {lo * 1000:.0f}-{hi * 1000:.0f}ms")
            for i, g in enumerate(gaps):
                if not (GAP_MIN <= g <= GAP_MAX):
                    bad.append(f"{name} gap {i}->{i+1} is {g * 1000:.0f}ms, "
                               f"outside 200-400ms")

        # 3. boundaries really are where the plan says
        quiet = deep_silences(audio)
        worst, worst_at = 0.0, -1
        for i in range(len(lines) - 1):
            edge = lines[i]["end"]
            if not quiet:
                bad.append(f"{name}: no silence found in the file at all")
                break
            near = min(quiet, key=lambda s: abs(s[0] - edge))
            off = abs(near[0] - edge)
            if off > worst:
                worst, worst_at = off, i
        print(f"  worst boundary error {worst:.3f}s (turn {worst_at})")
        if worst > BOUNDARY_TOL:
            bad.append(f"{name}: boundary {worst_at} is {worst:.3f}s from the "
                       f"silence it should sit on — the plan and the audio "
                       f"have come apart")

        # 4. duration
        real = duration(audio)
        planned = lines[-1]["end"]
        if abs(real - cv["duration"]) > 0.05:
            bad.append(f"{name}: json says {cv['duration']:.2f}s, file is {real:.2f}s")
        if abs(real - planned) > DURATION_TOL:
            bad.append(f"{name}: last turn ends at {planned:.2f}s but the file "
                       f"runs {real:.2f}s")

        words = sum(len(L["words"]) for L in lines)
        agent = sum(1 for L in lines if L["speaker"] == "agent")
        print(f"  {agent} agent / {len(lines) - agent} caller turns, {words} words\n")

    if bad:
        print("FAIL:")
        for b in bad:
            print("  " + b)
        return 1
    print("conversations and audio agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
