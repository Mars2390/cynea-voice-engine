"""Cynea Voice Engine — SSML voice quality test.

Generates three phrases that exercise different prosody-detection rules
in cynea_africa/synthesizer/edge_tts.py and saves them as MP3s next to
the existing baseline files. A/B against the prior `kwame_test_*.mp3`
or `amina_test_*.mp3` files to hear the difference.

Phrase coverage
---------------
1. Multi-sentence greeting with question intonation
   - Triggers: sentence-end breaks, question pitch (+10%), filler rule
     not fired (no "Um/Ah" first-token).

2. Disfluency + numerics + self-correction
   - Triggers: first-word "Um" filler break, "actually" mid-sentence
     break, per-token rate=0.9 on "480 cedis", sentence-end breaks.

3. Apology with empathy
   - Triggers: sentence-level rate=0.85 + volume=soft on the "Sorry…"
     opener, normal prosody on the action sentence, slow numerics if
     any.

Output:
    examples/_out/ssml_test_1.mp3
    examples/_out/ssml_test_2.mp3
    examples/_out/ssml_test_3.mp3

Run:
    python examples/test_ssml_voice.py

Requires:
    pip install edge-tts
"""

from __future__ import annotations

import asyncio
import os
import sys

# Windows consoles default to cp1252; reconfigure so the script never
# crashes on em-dashes / non-ASCII.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except (AttributeError, OSError):
    pass


VOICE = "en-GB-RyanNeural"
SPEED = 0.95   # Combined with per-segment SSML rate via the synthesizer.

PHRASES = [
    (
        "greeting_with_question",
        "Hello? Yes, Adinkra Hotel. Kwame speaking. How can I help?",
    ),
    (
        "disfluency_and_numerics",
        "Um, let me check... actually, the rate is 480 cedis. That's breakfast included.",
    ),
    (
        "apology_with_empathy",
        "Sorry, that shouldn't have happened. Let me put you through to my supervisor right away.",
    ),
]

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")


def _check_edge_tts_installed() -> bool:
    try:
        import edge_tts  # noqa: F401
        return True
    except ImportError:
        return False


async def main() -> int:
    if not _check_edge_tts_installed():
        print("edge-tts is not installed. Run: pip install edge-tts")
        return 1

    from cynea.models import SynthesisRequest
    from cynea_africa.synthesizer.edge_tts import EdgeTTSSynthesizer

    os.makedirs(OUT_DIR, exist_ok=True)
    synth = EdgeTTSSynthesizer(voice=VOICE)

    print("=" * 64)
    print("  CYNEA VOICE ENGINE — SSML PROSODY TEST")
    print(f"  Voice: {VOICE} @ base speed {SPEED}")
    print(f"  Out:   {OUT_DIR}")
    print("=" * 64)

    health = await synth.health_check(timeout=2.0)
    if not health.get("ready"):
        print(f"\nEdge TTS not ready: {health.get('reason') or 'unknown reason'}")
        return 1

    failures = 0
    for index, (label, phrase) in enumerate(PHRASES, start=1):
        out_path = os.path.join(OUT_DIR, f"ssml_test_{index}.mp3")

        print(f"\n[{index}/{len(PHRASES)}] {label}")
        print(f"  text: {phrase}")

        # Print the auto-generated SSML so the operator can sanity-check
        # which rules fired before the audio is produced.
        ssml = EdgeTTSSynthesizer.text_to_ssml(phrase)
        print(f"  ssml: {ssml}")

        request = SynthesisRequest(text=phrase, voice=VOICE, speed=SPEED)
        try:
            audio = await synth.synthesize(request)
        except ImportError as exc:
            audio = b""
            print(f"  -> package missing: {exc}")
        except ConnectionError as exc:
            audio = b""
            print(f"  -> no network: {exc}")
        except RuntimeError as exc:
            audio = b""
            print(f"  -> synthesis failed: {exc}")
        except Exception as exc:
            audio = b""
            print(f"  -> unexpected error: {exc!r}")

        if not audio:
            failures += 1
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

    print("\n" + "=" * 64)
    if failures == 0:
        print(f"  Done. A/B against the previous flat-prosody MP3s in {OUT_DIR}.")
        print("  The SSML versions should sound more natural — clearer pauses,")
        print("  rising intonation on the question, slower delivery on the price")
        print("  and on the apology.")
        return 0
    print(f"  Done with {failures} failure(s) of {len(PHRASES)}.")
    return 2


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)
