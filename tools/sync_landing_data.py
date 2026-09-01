#!/usr/bin/env python3
"""Rewrite the landing page's chat-demo data from the segment manifest.

index.html carries a one-line `var DATA={...}` blob so the scripted chat
works from a static file with no fetch and no build step. That blob is a
copy of assets/segments/manifest.json, and a copy drifts: re-cut a segment,
correct a transcript, and the page keeps quoting the old text.

    python tools/cut_segments.py        # writes the manifest
    python tools/sync_landing_data.py   # copies it into index.html

Run after any change to the manifest. `--check` verifies the page is in
sync without writing, which is what CI should call.

The manifest owns audio: file, duration, transcript, triggers, fallback.
This file owns only what is presentational and has nowhere else to live —
the tab label and the suggestion chips.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEGDIR = ROOT / "assets" / "segments"
MANIFEST = SEGDIR / "manifest.json"
CAPTIONS = SEGDIR / "captions.json"
PAGE_CAPTIONS = SEGDIR / "captions.page.json"
PAGE = ROOT / "index.html"

# Page-only copy, keyed by agent. `suggest` seeds the chips under the input
# and doubles as the placeholder text, so keep the first entry a greeting.
PRESENTATION: dict[str, dict] = {
    "kwame": {
        "role": "Hotel Receptionist",
        "suggest": ["Hello", "What are your rates?", "I want to book a room", "Thank you"],
    },
    "amina": {
        "role": "Bank Support",
        "suggest": ["Hello", "What's my balance?", "I need a transfer", "Thanks, bye"],
    },
    "kofi": {
        "role": "Restaurant Orders",
        "suggest": ["Hello", "I want to order jollof", "How long is delivery?", "Thank you"],
    },
    "maya": {
        "role": "Bookings & Scheduling",
        "suggest": ["Hello", "What slots are available?", "Confirm my booking", "Thanks, bye"],
    },
}

# The line this script owns. Anchored to the whole line so it cannot match
# any other assignment in the file.
LINE = re.compile(r"^(?P<indent>[ \t]*)var DATA=\{.*\};[ \t]*$", re.MULTILINE)

# Agent order on the page: the tab strip reads left to right in this order,
# and the first one is selected on load.
ORDER = ["kwame", "amina", "kofi", "maya"]


def build() -> str:
    """The DATA object, as compact JSON."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    agents: dict[str, dict] = {}

    for name in ORDER:
        entry = manifest["agents"].get(name)
        if entry is None:
            raise SystemExit(f"{name!r} is in ORDER but not in the manifest")
        page = PRESENTATION.get(name)
        if page is None:
            raise SystemExit(f"{name!r} has no PRESENTATION entry")

        # [file, duration, text] rather than an object: this line ships to
        # every visitor, and the keys would be a third of its weight.
        agents[name] = {
            "role": page["role"],
            "fallback": entry.get("fallback", "greeting"),
            "suggest": page["suggest"],
            "seg": {
                intent: [seg["file"], seg["duration"], seg["text"]]
                for intent, seg in entry["segments"].items()
            },
        }

    data = {"triggers": manifest["triggers"], "agents": agents}
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def page_captions(html: str) -> tuple[str, int]:
    """The caption tracks for the audio this page can actually play.

    captions.json carries every track including the full recordings, most of
    which the page never loads. Shipping all of it would put ~100 KB of
    transcript on the wire for clips nobody can start from here, so the page
    gets a subset, minified, fetched on first play rather than at load.
    """
    if not CAPTIONS.exists():
        return "", 0

    everything = json.loads(CAPTIONS.read_text(encoding="utf-8"))["tracks"]
    played = set(re.findall(r"assets/[A-Za-z0-9_./-]+\.mp3", html))
    subset = {path: track for path, track in everything.items() if path in played}

    # The two stitched conversations carry their own timings in
    # assets/segments/conversations.json, written by
    # tools/build_conversations.py, and are never in captions.json. Warning
    # about them on every run would train the reader to ignore this warning,
    # which is the only thing standing between a silently uncaptioned clip
    # and production.
    own_captions = {path for path in played if path.endswith("_full_conversation.mp3")}
    missing = sorted(played - set(subset) - own_captions)
    for path in missing:
        print(f"  no captions for {path}", file=sys.stderr)

    return json.dumps(subset, ensure_ascii=False, separators=(",", ":")), len(subset)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="fail if index.html is out of date; write nothing")
    args = ap.parse_args()

    if not MANIFEST.exists():
        print(f"missing {MANIFEST}. Run tools/cut_segments.py first.", file=sys.stderr)
        return 1

    html = PAGE.read_text(encoding="utf-8")
    match = LINE.search(html)
    if match is None:
        print("no `var DATA={...};` line found in index.html — has the chat "
              "demo been renamed or removed?", file=sys.stderr)
        return 1

    fresh = f"{match.group('indent')}var DATA={build()};"
    captions, tracks = page_captions(html)
    stale_captions = bool(captions) and (
        not PAGE_CAPTIONS.exists()
        or PAGE_CAPTIONS.read_text(encoding="utf-8") != captions
    )

    if match.group(0) == fresh and not stale_captions:
        print("index.html and captions.page.json are in sync.")
        return 0

    if args.check:
        print("landing data is OUT OF DATE. Run: python tools/sync_landing_data.py",
              file=sys.stderr)
        return 1

    if stale_captions:
        PAGE_CAPTIONS.write_text(captions, encoding="utf-8")
        print(f"wrote {PAGE_CAPTIONS.relative_to(ROOT)} — {tracks} tracks, "
              f"{len(captions) / 1024:.0f} KB")

    if match.group(0) == fresh:
        return 0

    PAGE.write_text(html[:match.start()] + fresh + html[match.end():], encoding="utf-8")
    agents = json.loads(MANIFEST.read_text(encoding="utf-8"))["agents"]
    total = sum(len(a["segments"]) for a in agents.values())
    print(f"updated index.html — {len(ORDER)} agents, {total} segments, "
          f"{len(fresh):,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
