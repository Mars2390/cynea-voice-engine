#!/usr/bin/env python3
"""Turn a real recording into the radial tick lengths of the Cynea voice mark.

    python tools/make_voicemark.py                     # print the SVG ticks
    python tools/make_voicemark.py --clip assets/segments/amina/greeting.mp3

The mark in the statement banner is not a generic ring of dots. Its tick
lengths are the amplitude envelope of an actual sentence one of the agents
speaks — Kwame's greeting by default — measured here and baked into the SVG
so the page ships no audio decoding of its own.

That is the whole point of it: a competitor can copy a rotating dot, but the
shape of this one is the shape of a specific person saying a specific thing,
which is also the claim the site is making. Re-run it against a different
clip and paste the new `<g>` block into index.html if the mark ever needs to
change.

Needs ffmpeg on PATH; no Python audio dependency.
"""

from __future__ import annotations

import argparse
import math
import pathlib
import struct
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "assets" / "segments" / "kwame" / "greeting.mp3"

TICKS = 40          # ticks around the circle
INNER = 23.0        # SVG units: where a tick starts
MIN_LEN = 8.0       # a silent bucket still shows a mark, so the ring stays whole
MAX_LEN = 26.0
ACCENT_AT = None    # filled in with the loudest bucket


def envelope(path: pathlib.Path, buckets: int) -> list[float]:
    """RMS amplitude per bucket, normalised to 0..1."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
        capture_output=True, check=True).stdout

    samples = struct.unpack(f"<{len(raw) // 2}h", raw[:len(raw) // 2 * 2])
    if not samples:
        sys.exit(f"no audio decoded from {path}")

    size = max(1, len(samples) // buckets)
    out = []
    for i in range(buckets):
        chunk = samples[i * size:(i + 1) * size] or (0,)
        out.append(math.sqrt(sum(s * s for s in chunk) / len(chunk)))

    peak = max(out) or 1.0
    # Square root compresses the range: speech is spiky, and a linear map
    # leaves most ticks at the floor with two spikes, which reads as noise
    # rather than as a voice.
    return [math.sqrt(v / peak) for v in out]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clip", default=str(DEFAULT))
    ap.add_argument("--ticks", type=int, default=TICKS)
    args = ap.parse_args()

    clip = pathlib.Path(args.clip)
    if not clip.exists():
        sys.exit(f"missing {clip}")

    levels = envelope(clip, args.ticks)
    loudest = max(range(len(levels)), key=lambda i: levels[i])

    print(f"<!-- {clip.relative_to(ROOT)} — {args.ticks} amplitude buckets, "
          f"regenerate with tools/make_voicemark.py -->")
    for i, level in enumerate(levels):
        angle = (360.0 / len(levels)) * i - 90.0
        rad = math.radians(angle)
        length = MIN_LEN + (MAX_LEN - MIN_LEN) * level
        x1, y1 = 50 + INNER * math.cos(rad), 50 + INNER * math.sin(rad)
        x2, y2 = 50 + (INNER + length) * math.cos(rad), 50 + (INNER + length) * math.sin(rad)
        cls = ' class="lead"' if i == loudest else ""
        print(f'<line{cls} x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"'
              f' style="--d:{i * 0.045:.2f}s"/>')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
