"""Drive one real conversation turn end to end.

    python examples/test_conversation.py                    # speak into the mic
    python examples/test_conversation.py --turns 3
    python examples/test_conversation.py --from-file assets/kwame_test_1.mp3
    python examples/test_conversation.py --persona amina --no-db

What it does, in order:

    1. record from the microphone      (or decode --from-file)
    2. transcribe with Groq Whisper    -- interim results print as they land
    3. send the transcript to the LLM
    4. synthesise the reply            -- edge_tts / ElevenLabs
    5. play it through the speakers    (or save it, with --no-play)
    6. write the call to Postgres      (skipped with --no-db)

and prints the full transcript at the end.

This is a laptop harness, not the call path. Real calls get their audio
from the carrier socket, which is why the engine takes an `on_audio` sink
rather than touching the sound card itself.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cynea  # noqa: E402  — loads .env
from cynea import audio as local_audio  # noqa: E402
from cynea.agent_loader import AgentLoader, _PERSONAS  # noqa: E402
from cynea.engine import EngineError  # noqa: E402
from cynea.models import AudioChunk  # noqa: E402

RULE = "=" * 68


def banner(title: str) -> None:
    print(f"\n{RULE}\n  {title}\n{RULE}")


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}")


# ----------------------------------------------------------------------
# Audio input
# ----------------------------------------------------------------------

def load_file(path: str) -> AudioChunk:
    """Decode any audio file to the 16 kHz mono PCM the engine expects."""
    try:
        from pydub import AudioSegment
    except ImportError:
        sys.exit("--from-file needs pydub:  pip install pydub  (plus ffmpeg)")

    seg = (AudioSegment.from_file(path)
           .set_frame_rate(local_audio.SAMPLE_RATE)
           .set_channels(1)
           .set_sample_width(2))
    print(f"  loaded {path}  ({seg.duration_seconds:.1f}s)")
    return AudioChunk(data=seg.raw_data, sample_rate=local_audio.SAMPLE_RATE,
                      channels=1, encoding="pcm16")


def capture(seconds: float) -> AudioChunk:
    print(f"  Speak now — ask about a room, an order, a booking.")
    chunk = local_audio.record(seconds=seconds)
    peak = local_audio.peak_level(chunk)
    bar = "#" * min(40, peak * 40 // 32767)
    print(f"  peak level: {peak:5d}/32767  |{bar:<40}|")
    if peak < 200:
        print("  -> That is near-silence. Check the mic is unmuted and is the "
              "default input device;\n     `python -c \"import cynea.audio as a; "
              "print(a.describe_devices())\"` lists them.")
    return chunk


# ----------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------

def resolve_agent(persona: str, email: str | None):
    """Find a database agent to attach the call to. None means no persistence."""
    try:
        from cynea import db
        if not os.getenv("DATABASE_URL"):
            print("  DATABASE_URL not set — running without persistence")
            return None
        if not db.healthcheck():
            print("  database unreachable — running without persistence")
            return None

        agents = []
        if email:
            user = db.get_user_by_email(email)
            if user is None:
                print(f"  no account {email} — running without persistence")
                return None
            agents = db.get_agents_by_user(user.id)
        else:
            with db.session_scope() as s:
                from sqlalchemy import select
                agents = list(s.scalars(select(db.Agent)))

        match = [a for a in agents if a.persona == persona]
        if not match:
            print(f"  no '{persona}' agent in the database — running without "
                  f"persistence\n     provision one with: python -m cynea.seed "
                  f"--email you@cynea.ai")
            return None

        print(f"  agent {match[0].id} ({match[0].name})")
        return match[0]
    except Exception as exc:
        print(f"  database unavailable ({type(exc).__name__}) — no persistence")
        return None


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

async def run(args) -> int:
    banner("CYNEA — LIVE CONVERSATION TEST")

    caps = local_audio.is_available()
    print(f"  microphone : {'yes' if caps['input'] else 'no'}")
    print(f"  speakers   : {'yes' if caps['output'] else 'no'}")
    print(f"  ffmpeg     : {'yes' if caps['ffmpeg'] else 'no'}")
    print(f"  providers  : {cynea.providers.registered()}")

    if args.persona not in _PERSONAS:
        sys.exit(f"Unknown persona {args.persona!r}. Have: {sorted(_PERSONAS)}")

    step(1, "Resolving agent")
    agent = None if args.no_db else resolve_agent(args.persona, args.email)

    # --- build the engine ---------------------------------------------
    played: list[float] = []

    def speaker_sink(data: bytes, fmt: str) -> None:
        """Where synthesised speech goes on this machine."""
        if args.no_play or not caps["output"]:
            path = f"reply_{len(played) + 1}.{fmt}"
            local_audio.save(data, path)
            print(f"      audio saved -> {path} ({len(data):,} bytes)")
            played.append(0.0)
            return
        seconds = local_audio.play(data, fmt=fmt)
        played.append(seconds)
        print(f"      audio played ({seconds:.1f}s, {len(data):,} bytes)")

    engine = AgentLoader().load_from_dict({
        "agent_name": f"{args.persona}-live",
        "persona": args.persona,
        "client_name": args.client,
        "llm_provider": "groq",
        "stt_provider": args.stt,
    })
    engine.on_audio = speaker_sink
    if agent is not None:
        engine.agent_id = agent.id
        engine.caller_number = args.number
        engine.persist = True
        from cynea_africa.dashboard.metrics import CallRecord, RateCard
        import uuid
        engine._metrics = CallRecord(call_id=str(uuid.uuid4()), agent=args.persona)
        engine._rate_card = RateCard.default_africa()

    name = args.persona.title()

    # --- greeting ------------------------------------------------------
    step(2, f"{name} answers")
    t0 = time.perf_counter()
    greeting = await engine.start()
    print(f"  {name}: {greeting.text}")
    print(f"      synthesised in {(time.perf_counter() - t0) * 1000:.0f}ms")

    # --- conversation turns --------------------------------------------
    for turn_no in range(1, args.turns + 1):
        step(2 + turn_no, f"Your turn ({turn_no}/{args.turns})")

        if args.from_file:
            chunk = load_file(args.from_file)
        elif caps["input"]:
            chunk = capture(args.seconds)
        else:
            print("  No microphone on this machine. Use --from-file <path>.")
            return 1

        # Interim transcripts, so you can see recognition happening.
        if args.show_interim and args.stt == "groq-stt":
            from cynea_africa.transcriber.groq_stt import GroqSTT

            async def one_shot():
                step_bytes = int(local_audio.SAMPLE_RATE * 0.2) * 2
                for i in range(0, len(chunk.data), step_bytes):
                    yield AudioChunk(data=chunk.data[i:i + step_bytes],
                                     sample_rate=chunk.sample_rate)
                    await asyncio.sleep(0.2)      # pace it like a live mic

            print("  transcribing:")
            async for partial in GroqSTT().stream_transcribe(one_shot()):
                tag = "final  " if partial.is_final else "interim"
                print(f"      [{tag}] {partial.text}")

        t0 = time.perf_counter()
        try:
            result = await engine.process_audio(chunk)
        except EngineError as exc:
            print(f"\n  {type(exc).__name__}: {exc}")
            return 1

        if result is None:
            print("  Nothing intelligible was said — no reply generated.")
            print("     (silence and barge-in return None; a provider failure raises)")
            continue

        elapsed = (time.perf_counter() - t0) * 1000
        print(f"\n  Caller: {result.user_text}")
        print(f"  {name}: {result.text}")
        print(f"      round trip {elapsed:.0f}ms  |  audio {len(result.audio):,} bytes")
        if result.call_id:
            print(f"      call row {result.call_id}")

    # --- close ----------------------------------------------------------
    call_id = engine.end_call() if engine.persist else None

    # --- PART 4: verify -------------------------------------------------
    banner("TRANSCRIPT")
    dialogue = engine.dialogue_text()
    print(dialogue if dialogue else "  (nothing was said)")

    banner("RESULT")
    turns = [m for m in engine.history.messages if m.get("role") in ("user", "assistant")]
    print(f"  Turns        : {len(turns)}")
    print(f"  Audio        : {'played ' + chr(10003) if any(played) else 'saved to file'}"
          f"  ({len(played)} clip{'s' if len(played) != 1 else ''},"
          f" {sum(played):.1f}s)")
    print(f"  STT provider : {args.stt}")
    print(f"  LLM provider : groq / {os.getenv('GROQ_MODEL', 'openai/gpt-oss-20b')}")
    print(f"  TTS provider : {engine.config.tts_provider} / {engine.config.voice}")

    if call_id:
        from cynea import db
        row = db.get_call_by_id(call_id)
        print(f"  Saved to DB  : {chr(10003)} {call_id}")
        print(f"                 {row.duration_s}s, sentiment {row.sentiment_score}, "
              f"{row.cost_cents}c, {row.status}")
    else:
        print(f"  Saved to DB  : no (not persisted)")

    print(f"\n{RULE}\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", default="kwame", help="kwame | amina | kofi | maya")
    ap.add_argument("--client", default="Adinkra Hotel", help="client name in the prompt")
    ap.add_argument("--seconds", type=float, default=5.0, help="mic recording length")
    ap.add_argument("--turns", type=int, default=1, help="how many exchanges")
    ap.add_argument("--from-file", metavar="PATH",
                    help="use an audio file instead of the microphone")
    ap.add_argument("--stt", default="groq-stt", help="groq-stt | groq_whisper | whisper")
    ap.add_argument("--number", default="+254700000000", help="caller number to record")
    ap.add_argument("--email", help="account whose agent to attach the call to")
    ap.add_argument("--no-db", action="store_true", help="skip persistence")
    ap.add_argument("--no-play", action="store_true", help="save audio instead of playing")
    ap.add_argument("--no-interim", dest="show_interim", action="store_false",
                    help="skip the interim transcription pass (halves STT cost)")
    args = ap.parse_args()

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except local_audio.AudioDeviceError as exc:
        print(f"\nAudio device problem:\n{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
