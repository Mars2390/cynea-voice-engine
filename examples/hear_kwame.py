"""Cynea Voice Engine — hear Kwame.

Generates real MP3 audio of Kwame (the Adinkra Hotel agent) speaking three
natural phrases, so you can hear what the voice actually sounds like
before wiring it into a phone call.

Output:
    examples/kwame_test_1.mp3
    examples/kwame_test_2.mp3
    examples/kwame_test_3.mp3

Run:
    python examples/hear_kwame.py

Requires:
    pip install edge-tts

Edge TTS uses Microsoft's free Azure Cognitive Services voice endpoint
over HTTPS. No API key needed, but it does require an internet connection.
"""

from __future__ import annotations

import asyncio
import os
import sys

# Windows consoles default to cp1252 and choke on em-dashes / non-ASCII.
# Reconfigure stdout so the script never crashes on print().
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except (AttributeError, OSError):
    pass


VOICE = "en-GB-RyanNeural"   # British male; closest neural voice to Ghanaian on Edge
SPEED = 0.95                 # Slightly slower — sounds more natural on the phone

PHRASES = [
    "Hello? Yes, Adinkra Hotel. Kwame speaking. How can I help?",
    "Ah, let me check... yes, we have a double room available for Friday. Two adults?",
    "It's four hundred and eighty cedis per night, breakfast included. Want me to hold it?",
]


def _check_edge_tts_installed() -> bool:
    """Return True if the edge-tts package can be imported."""
    try:
        import edge_tts  # noqa: F401
        return True
    except ImportError:
        return False


async def main() -> int:
    if not _check_edge_tts_installed():
        print("edge-tts is not installed.")
        print("Install it and re-run:")
        print("    pip install edge-tts")
        return 1

    # Imported here so the friendlier error above runs first if edge-tts is missing.
    from cynea.models import SynthesisRequest
    from cynea_africa.synthesizer.edge_tts import EdgeTTSSynthesizer

    out_dir = os.path.dirname(os.path.abspath(__file__))
    synthesizer = EdgeTTSSynthesizer(voice=VOICE)

    print("=" * 60)
    print("  CYNEA VOICE ENGINE — HEAR KWAME")
    print(f"  Voice: {VOICE} @ speed {SPEED}")
    print("=" * 60)

    failures = 0
    for index, phrase in enumerate(PHRASES, start=1):
        out_path = os.path.join(out_dir, f"kwame_test_{index}.mp3")
        request = SynthesisRequest(text=phrase, voice=VOICE, speed=SPEED)

        print(f"\n[{index}/{len(PHRASES)}] Synthesizing: {phrase}")

        try:
            audio = await synthesizer.synthesize(request)
        except Exception as exc:
            audio = b""
            print(f"  -> synthesis raised: {exc}")

        if not audio:
            failures += 1
            print(f"  -> FAILED (no audio returned). Check your internet connection.")
            continue

        try:
            with open(out_path, "wb") as f:
                f.write(audio)
        except OSError as exc:
            failures += 1
            print(f"  -> FAILED to write {out_path}: {exc}")
            continue

        size_kb = len(audio) / 1024.0
        print(f"  -> wrote {out_path} ({size_kb:,.1f} KB)")

    print("\n" + "=" * 60)
    if failures == 0:
        print(f"  Done. Open the .mp3 files in {out_dir} to hear Kwame.")
        return 0
    print(f"  Done with {failures} failure(s) of {len(PHRASES)}.")
    return 2


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)
