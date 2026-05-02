"""Cynea Voice Engine — hear Kwame (ElevenLabs edition).

Generates real MP3 audio of Kwame (the Adinkra Hotel agent) speaking
three natural phrases via ElevenLabs, so you can preview the premium
voice quality before wiring it into a live call.

Output:
    examples/_out/kwame_test_1.mp3
    examples/_out/kwame_test_2.mp3
    examples/_out/kwame_test_3.mp3

(The MP3s land in `_out/` so the marketing landing page can play them
inline via its "Hear demo" button.)

Run:
    python examples/hear_kwame.py

Requires:
    pip install elevenlabs python-dotenv

Configuration:
    Add the following to your .env file at the repo root:
        ELEVENLABS_API_KEY=sk_...
"""

from __future__ import annotations

import asyncio
import os
import sys

# Windows consoles default to cp1252 and choke on em-dashes / non-ASCII.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except (AttributeError, OSError):
    pass


# Cynea-shipped Kwame voice. George — British male, warm. Premium tier.
VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
SPEED = 0.95   # Carried through the SynthesisRequest; ElevenLabs' turbo
               # model handles its own pacing internally, but we pass
               # the value through for parity with the Edge path.

PHRASES = [
    # Booking step 1 — greeting (mapped to kwame_test_1.mp3 by the
    # landing-page chat widget).
    "Hello? Yes, Adinkra Hotel. Kwame speaking. How can I help you today?",
    # Booking step 2 — room inquiry response (kwame_test_2.mp3).
    # Numerics spelled out so ElevenLabs renders them naturally; the
    # turbo model otherwise reads "$80" as "eighty dollar sign" on
    # some pronunciations.
    "Ah, lovely! When would you like to check in? We have standard at eighty dollars, deluxe at one hundred twenty, and executive suites at two hundred per night.",
    # Booking step 3 — availability + price + hold offer (kwame_test_3.mp3).
    "Let me check... yes, we have a deluxe room available this Friday. That's one hundred twenty dollars per night, breakfast included. Shall I hold the booking for you?",
]

# Output dir — examples/_out/ so the landing page's relative audio
# paths resolve correctly.
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")


def _check_deps_installed() -> tuple:
    """Return (ok, missing_list)."""
    missing = []
    try:
        import elevenlabs  # noqa: F401
    except ImportError:
        missing.append("elevenlabs")
    try:
        import dotenv  # noqa: F401
    except ImportError:
        missing.append("python-dotenv")
    return (not missing, missing)


async def main() -> int:
    ok, missing = _check_deps_installed()
    if not ok:
        print("Required packages not installed:")
        print(f"    pip install {' '.join(missing)}")
        return 1

    # Lazy imports so the friendlier message above fires first.
    from cynea.models import SynthesisRequest
    from cynea_africa.synthesizer.elevenlabs_synthesizer import ElevenLabsSynthesizer

    os.makedirs(OUT_DIR, exist_ok=True)
    synthesizer = ElevenLabsSynthesizer(voice=VOICE_ID)

    print("=" * 64)
    print("  CYNEA VOICE ENGINE — HEAR KWAME (ElevenLabs)")
    print(f"  Voice: {VOICE_ID}  ({synthesizer.VOICES.get(VOICE_ID, 'custom')})")
    print(f"  Model: {synthesizer.model}")
    print(f"  Out:   {OUT_DIR}")
    print("=" * 64)

    health = await synthesizer.health_check(timeout=2.0)
    if not health.get("ready"):
        print(f"\nElevenLabs is not ready: {health.get('reason') or 'unknown reason'}")
        print("Common fixes:")
        print("  - Add ELEVENLABS_API_KEY=sk_... to your .env file.")
        print("  - Confirm your network can reach api.elevenlabs.io:443.")
        return 1

    failures = 0
    for index, phrase in enumerate(PHRASES, start=1):
        out_path = os.path.join(OUT_DIR, f"kwame_test_{index}.mp3")
        request = SynthesisRequest(text=phrase, voice=VOICE_ID, speed=SPEED)

        print(f"\n[{index}/{len(PHRASES)}] Synthesizing: {phrase}")

        try:
            audio = await synthesizer.synthesize(request)
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
        print(f"  Done. Open the .mp3 files in {OUT_DIR} to hear Kwame.")
        print("  The landing page's 'Hear demo' button will now play them.")
        return 0
    print(f"  Done with {failures} failure(s) of {len(PHRASES)}.")
    return 2


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)
