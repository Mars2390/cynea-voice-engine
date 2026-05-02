"""Cynea Voice Engine — hear Amina (ElevenLabs edition).

Generates real MP3 audio of Amina (the Kenyan customer-service agent)
speaking three natural phrases via ElevenLabs.

The MP3 files are written to examples/_out/ so the marketing landing
page (examples/_out/cynea_landing.html) can play them directly via
its "Hear demo" button.

Output:
    examples/_out/amina_test_1.mp3
    examples/_out/amina_test_2.mp3
    examples/_out/amina_test_3.mp3

Run:
    python examples/hear_amina.py

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


# Cynea-shipped Amina voice. Bella — American female, warm. Premium tier.
VOICE_ID = "EXAVITQu4vr4xnSDxMaL"
SPEED = 1.0   # Kenyan call-centre English is faster than Ghanaian; keep
              # baseline at 1.0. ElevenLabs' turbo model handles its own
              # pacing internally; we pass speed for interface parity.

PHRASES = [
    "Hello, this is Amina. How can I help you today?",
    "Let me check your account. Yes, I can see your balance. Is there anything else I can help with?",
    "I understand your frustration. Let me connect you with my manager right away.",
]

# Output dir — examples/_out/ so the landing page's relative audio
# paths resolve correctly.
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")


def _check_deps_installed() -> tuple:
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

    from cynea.models import SynthesisRequest
    from cynea_africa.synthesizer.elevenlabs_synthesizer import ElevenLabsSynthesizer

    os.makedirs(OUT_DIR, exist_ok=True)
    synthesizer = ElevenLabsSynthesizer(voice=VOICE_ID)

    print("=" * 64)
    print("  CYNEA VOICE ENGINE — HEAR AMINA (ElevenLabs)")
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
        out_path = os.path.join(OUT_DIR, f"amina_test_{index}.mp3")
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
        print(f"  Done. Open the .mp3 files in {OUT_DIR} to hear Amina.")
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
