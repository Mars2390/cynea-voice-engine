"""Cynea Voice Engine — hear Kofi (ElevenLabs edition).

Generates real MP3 audio of Kofi (the Asaase Restaurant order-taking
agent) speaking three natural phrases via ElevenLabs, so you can preview
the premium voice quality before wiring it into a live call.

Output:
    examples/_out/kofi_test_1.mp3
    examples/_out/kofi_test_2.mp3
    examples/_out/kofi_test_3.mp3

(The MP3s land in `_out/` so any landing-page widget that wants to
preview Kofi can reference them via relative path.)

Run:
    python examples/hear_kofi.py

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


# Cynea-shipped Kofi voice. ElevenLabs "George" — British male, warm.
# Same voice id Kwame uses; A/B swap with en-NG-AbeoNeural for a more
# West-African cadence if your callers prefer it.
VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
SPEED = 1.05   # Slightly faster than 1.0 — Kofi is an order-taker, not
               # a hospitality desk. Speed is carried through the
               # SynthesisRequest for interface parity; ElevenLabs'
               # turbo model handles its own pacing internally.

PHRASES = [
    # Greeting (mapped to kofi_test_1.mp3) — discloses AI-on-the-line in
    # Kofi's register and immediately cues the service-mode question.
    "Akwaaba! This is Kofi at Asaase Restaurant. Ready to take your order today? Are we doing dine-in, takeaway, or delivery?",
    # Order summary + price + upsell (kofi_test_2.mp3). Numerics spelled
    # out so ElevenLabs renders "fifty-three cedis" naturally rather
    # than dropping into a digit-by-digit cadence.
    "Ah, great choice! Jollof with chicken, medium spice, with a side of kelewele and a sobolo to drink. That comes to fifty-three cedis. Anything else?",
    # Confirmation + ETA (kofi_test_3.mp3).
    "Your order is confirmed! Number forty-seven. It'll be ready in about twenty-five minutes. We'll send you an SMS when it's on the way. Thank you!",
]

# Output dir — examples/_out/ so any landing-page widget's relative
# audio paths resolve correctly.
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
    print("  CYNEA VOICE ENGINE — HEAR KOFI (ElevenLabs)")
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
        out_path = os.path.join(OUT_DIR, f"kofi_test_{index}.mp3")
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
        print(f"  Done. Open the .mp3 files in {OUT_DIR} to hear Kofi.")
        return 0
    print(f"  Done with {failures} failure(s) of {len(PHRASES)}.")
    return 2


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)
