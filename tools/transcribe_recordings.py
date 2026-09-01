#!/usr/bin/env python3
"""Transcribe the four real-voice recordings with word-level timestamps.

Free and local. Uses faster-whisper (CTranslate2), which does *not* pull
torch — the ~2 GB that keeps `openai-whisper` out of requirements.txt.

    pip install faster-whisper
    python tools/transcribe_recordings.py

Writes assets/segments/transcripts.json:

    {"kwame": {"duration": 97.8,
               "words": [{"w": "Hello", "start": 0.12, "end": 0.44}, ...],
               "text": "Hello, Adinkra Hotel ..."}, ...}

That file is the input to tools/cut_segments.py, which turns word timings
into the actual segment boundaries. Run this once; the JSON is committed so
nobody else needs the model.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
OUT = ASSETS / "segments" / "transcripts.json"

RECORDINGS = {
    "kwame": "kwame_real_voice.mp3",
    "amina": "amina_real_voice.mp3",
    "kofi": "kofi_real_voice.mp3",
    "maya": "maya_real_voice.mp3",
}


def main() -> int:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print(
            "faster-whisper is not installed.\n"
            "  pip install faster-whisper      # free, local, no torch\n",
            file=sys.stderr,
        )
        return 1

    # "base" is enough to locate phrases; int8 keeps it on CPU comfortably.
    model = WhisperModel("base", device="cpu", compute_type="int8")

    out: dict[str, dict] = {}
    for name, filename in RECORDINGS.items():
        path = ASSETS / filename
        if not path.exists():
            print(f"missing {path}", file=sys.stderr)
            return 1

        print(f"transcribing {filename} ...", flush=True)
        segments, info = model.transcribe(str(path), word_timestamps=True, language="en")

        words, text = [], []
        for seg in segments:
            text.append(seg.text)
            for w in seg.words or []:
                words.append({"w": w.word.strip(), "start": round(w.start, 2),
                              "end": round(w.end, 2)})

        out[name] = {
            "file": filename,
            "duration": round(info.duration, 2),
            "text": "".join(text).strip(),
            "words": words,
        }
        print(f"  {len(words)} words, {info.duration:.1f}s")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
