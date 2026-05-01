"""Cynea Voice Engine — hear Amina.

Generates real MP3 audio of Amina (the Kenyan customer-service agent)
speaking three natural phrases, so you can preview the voice before
wiring it into a live call.

The MP3 files are written to examples/_out/ so the marketing landing
page (examples/_out/cynea_landing.html) can play them directly via its
"Hear demo" button — relative paths resolve from the HTML's directory.

Output:
    examples/_out/amina_test_1.mp3
    examples/_out/amina_test_2.mp3
    examples/_out/amina_test_3.mp3

Run:
    python examples/hear_amina.py

Requires:
    pip install edge-tts

Edge TTS uses Microsoft's free Azure Cognitive Services voice endpoint
over HTTPS. No API key needed, but it does require internet access.
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


VOICE = "en-GB-SoniaNeural"   # British female; closest warm female voice to Kenyan on Edge
SPEED = 1.0                   # Kenyans on the phone speak at normal pace, not slowed down

PHRASES = [
    "Hello, this is Amina. How can I help you today?",
    "Let me check your account. Yes, I can see your balance. Is there anything else I can help with?",
    "I understand your frustration. Let me connect you with my manager right away.",
]

# Output directory — examples/_out/ so the landing page's relative
# audio paths resolve correctly.
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")


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

    # Imported here so the friendlier message above runs first if edge-tts is missing.
    from cynea.models import SynthesisRequest
    from cynea_africa.synthesizer.edge_tts import EdgeTTSSynthesizer

    os.makedirs(OUT_DIR, exist_ok=True)
    synthesizer = EdgeTTSSynthesizer(voice=VOICE)

    print("=" * 60)
    print("  CYNEA VOICE ENGINE — HEAR AMINA")
    print(f"  Voice: {VOICE} @ speed {SPEED}")
    print(f"  Out:   {OUT_DIR}")
    print("=" * 60)

    # One health check up front saves three round-trips of useless 401/timeout work.
    health = await synthesizer.health_check(timeout=2.0)
    if not health.get("ready"):
        print(f"\nEdge TTS is not ready: {health.get('reason') or 'unknown reason'}")
        print("Common fixes:")
        print("  - Check your internet connection.")
        print("  - Confirm your firewall allows speech.platform.bing.com:443.")
        return 1

    failures = 0
    for index, phrase in enumerate(PHRASES, start=1):
        out_path = os.path.join(OUT_DIR, f"amina_test_{index}.mp3")
        request = SynthesisRequest(text=phrase, voice=VOICE, speed=SPEED)

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

    print("\n" + "=" * 60)
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
