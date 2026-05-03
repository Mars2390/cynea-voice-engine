"""Cynea Voice Engine — generate Kwame's full 9-response demo audio.

Renders every Kwame response from the 17-turn anniversary-booking
demo (examples/_out/demo_call_flow.html) as ElevenLabs MP3s. Once
generated, the demo page can drop its Web Speech API fallback for
Kwame's later turns and play these polished recordings instead.

Output:
    examples/_out/demo_kwame_01.mp3   greeting
    examples/_out/demo_kwame_02.mp3   anniversary opener
    examples/_out/demo_kwame_03.mp3   executive suite recommendation
    examples/_out/demo_kwame_04.mp3   dining + dinner offer
    examples/_out/demo_kwame_05.mp3   airport shuttle
    examples/_out/demo_kwame_06.mp3   booking summary + total
    examples/_out/demo_kwame_07.mp3   payment details capture
    examples/_out/demo_kwame_08.mp3   final confirmation
    examples/_out/demo_kwame_09.mp3   sign-off

Run:
    python examples/hear_kwame_demo.py

Requires:
    pip install elevenlabs python-dotenv

Configuration:
    Add the following to your .env file at the repo root:
        ELEVENLABS_API_KEY=sk_...

Estimated cost (Creator tier ~$0.30 per 1k chars):
    ~1,800 chars across 9 phrases ≈ $0.55 per full run.
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


# Cynea-shipped Kwame voice. ElevenLabs "George" — British male, warm.
VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
SPEED = 0.95   # Carried through the SynthesisRequest for interface
               # parity; ElevenLabs' turbo model handles its own pacing
               # internally.

# (output_filename, phrase_text) — order matches the demo's TURN
# numbering. Numerics deliberately spelled out ("two hundred dollars",
# "March fifteen to eighteen") so ElevenLabs renders them naturally
# rather than dropping into a digit-by-digit cadence on currency or
# date strings.
PHRASES = [
    (
        "demo_kwame_01.mp3",
        "Hello? Adinkra Hotel, Accra. Kwame speaking. How can I help?",
    ),
    (
        "demo_kwame_02.mp3",
        "Ah, anniversary! Congratulations! You've come to the right place. "
        "We have some lovely rooms. Are you thinking ocean view or city view? "
        "And what dates are you looking at?",
    ),
    (
        "demo_kwame_03.mp3",
        "Let me check for you... Yes, we have availability. For a special "
        "anniversary, I would recommend our Executive Suite. King bed, "
        "panoramic ocean view, private balcony, and we can arrange champagne "
        "and flowers for your arrival. That is two hundred dollars per night.",
    ),
    (
        "demo_kwame_04.mp3",
        "Yes! Our restaurant serves Ghanaian and continental cuisine. We can "
        "also arrange a private candlelit dinner on your balcony. Breakfast "
        "is complimentary — fresh fruit, eggs, our famous hausa koko porridge. "
        "Would you like me to book the dinner as well?",
    ),
    (
        "demo_kwame_05.mp3",
        "We have airport shuttle service — twenty-five dollars each way, "
        "about thirty minutes from Kotoka International. I can arrange pickup "
        "for your flight. What time do you arrive on the fifteenth?",
    ),
    (
        "demo_kwame_06.mp3",
        "Perfect. Let me summarize: Executive Suite, March fifteen to eighteen, "
        "three nights at two hundred per night comes to six hundred dollars. "
        "Private dinner on the fifteenth, airport pickup at two in the afternoon. "
        "Champagne and flowers on arrival. Total including shuttle and dinner "
        "comes to approximately seven hundred and twenty dollars. Shall I go "
        "ahead and secure this?",
    ),
    (
        "demo_kwame_07.mp3",
        "Lovely! I will need your full name, a phone number, an email for the "
        "confirmation, and a deposit equal to the first night — two hundred "
        "dollars. We accept Visa, Mastercard, or Mobile Money.",
    ),
    (
        "demo_kwame_08.mp3",
        "Thank you, David. So to confirm: David Osei, Executive Suite, "
        "March fifteen to eighteen, airport pickup at two in the afternoon, "
        "anniversary dinner, champagne and flowers. Total seven hundred and "
        "twenty dollars. Deposit two hundred dollars charged to your card. "
        "You will receive a confirmation email shortly. Is everything correct?",
    ),
    (
        "demo_kwame_09.mp3",
        "You are most welcome, David. We look forward to making your "
        "anniversary special. Safe flight, and we will see you on the "
        "fifteenth. Akwaaba!",
    ),
]

# Output dir — examples/_out/ so the demo_call_flow.html landing-page
# widget can play these via relative-path <audio> elements.
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

    total_chars = sum(len(text) for _, text in PHRASES)
    print("=" * 64)
    print("  CYNEA VOICE ENGINE — DEMO CALL: KWAME (full set)")
    print(f"  Voice: {VOICE_ID}  ({synthesizer.VOICES.get(VOICE_ID, 'custom')})")
    print(f"  Model: {synthesizer.model}")
    print(f"  Out:   {OUT_DIR}")
    print(f"  Phrases: {len(PHRASES)}  ·  ~{total_chars:,} characters")
    print("=" * 64)

    health = await synthesizer.health_check(timeout=2.0)
    if not health.get("ready"):
        print(f"\nElevenLabs is not ready: {health.get('reason') or 'unknown reason'}")
        print("Common fixes:")
        print("  - Add ELEVENLABS_API_KEY=sk_... to your .env file.")
        print("  - Confirm your network can reach api.elevenlabs.io:443.")
        return 1

    failures = 0
    total_bytes = 0
    for index, (filename, phrase) in enumerate(PHRASES, start=1):
        out_path = os.path.join(OUT_DIR, filename)
        request = SynthesisRequest(text=phrase, voice=VOICE_ID, speed=SPEED)

        # Truncate the displayed phrase so the per-line log stays scannable.
        preview = phrase if len(phrase) <= 88 else phrase[:85] + "..."
        print(f"\n[{index}/{len(PHRASES)}] {filename}")
        print(f"        text:  {preview}")

        try:
            audio = await synthesizer.synthesize(request)
        except ImportError as exc:
            audio = b""
            print(f"        -> package missing: {exc}")
        except ConnectionError as exc:
            audio = b""
            print(f"        -> no network: {exc}")
        except RuntimeError as exc:
            audio = b""
            print(f"        -> synthesis failed: {exc}")
        except Exception as exc:
            audio = b""
            print(f"        -> unexpected error: {exc!r}")

        if not audio:
            failures += 1
            continue

        try:
            with open(out_path, "wb") as f:
                f.write(audio)
        except OSError as exc:
            failures += 1
            print(f"        -> FAILED to write {out_path}: {exc}")
            continue

        size_kb = len(audio) / 1024.0
        total_bytes += len(audio)
        print(f"        -> wrote {filename} ({size_kb:,.1f} KB)")

    print("\n" + "=" * 64)
    if failures == 0:
        size_mb = total_bytes / (1024 * 1024)
        print(f"  Done. {len(PHRASES)} files written ({size_mb:.2f} MB total).")
        print(f"  Open {OUT_DIR} to listen, or wire them into")
        print(f"  examples/_out/demo_call_flow.html as the per-turn audio map.")
        return 0
    print(f"  Done with {failures} failure(s) of {len(PHRASES)}.")
    return 2


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)
