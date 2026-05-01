"""Cynea Voice Engine — full-pipeline demo.

Simulates a phone conversation end-to-end without requiring a phone line,
an LLM API key, or a microphone. What it exercises:

  - cynea.conversation.ConversationHistory (committed/interim history,
    barge-in trim, tool-message sanitization)
  - cynea.interruption.InterruptionManager (sequence-id cancellation,
    word-threshold barge-in, grace period, backchannels)
  - cynea_africa.dashboard.metrics (cost-per-call, sentiment, CSV/JSON
    export)
  - cynea_africa.synthesizer.edge_tts (free Microsoft Edge TTS, optional
    — the demo skips the audio synthesis step if edge-tts is not
    installed, so it still runs in a clean venv)
  - cynea.providers.MockLLM as the LLM (deterministic, no API key)

Run it:
    python examples/full_call_demo.py

Output:
    - logs of each turn
    - metrics summary printed at the end
    - calls.json + calls.csv written to the current directory
    - if edge-tts is installed: kwame_turn_*.mp3 written for each agent turn

Note on Kwame's persona: the brief asked for an "undetectable human"
agent. We're not shipping that — see README.md for the legal/ethical
reasoning. This demo uses a disclosed-but-warm variant of the prompt.
The original prompt is still in cynea_africa/persona/kwame.py if you
want to run that path; just import KWAME_SYSTEM_PROMPT instead.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Optional

# Windows consoles default to cp1252, which breaks on em-dashes and arrows.
# Reconfigure stdout to UTF-8 with error fallback so the demo always runs.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except (AttributeError, OSError):
    pass

from cynea.conversation import ConversationHistory
from cynea.interruption import InterruptionManager
from cynea.models import AgentConfig, SynthesisRequest
from cynea.providers import MockLLM, get_llm_provider, get_tts_provider
from cynea_africa.dashboard.metrics import MetricsTracker, RateCard


# ---------------------------------------------------------------------
# Disclosed Kwame prompt — same warmth, no deception.
# ---------------------------------------------------------------------

KWAME_DISCLOSED_PROMPT = """\
You are Kwame, the AI assistant for Adinkra Hotel in Accra, Ghana.

You introduce yourself as the AI assistant on the very first turn so the
caller knows what they're speaking to. After that, you behave warmly and
naturally — like a real receptionist would. You handle bookings, room
information, restaurant hours, directions, and small talk.

VOICE:
- Brief. Most replies are one or two sentences.
- Warm. Greet by time of day.
- Natural disfluencies are fine ("Mm, let me check..." "One moment.").
- If you don't know, say so and offer to take a message.

NEVER:
- Claim to be human.
- Promise things you can't verify (rates, availability, food allergies).
- Use corporate-script phrases ("How may I assist you today?").

DISCLOSURE:
- First turn: "Hi, you've reached Adinkra Hotel in Accra. This is Kwame,
  the AI assistant — what can I help you with?"
- If asked "are you human?": acknowledge you're an AI, but stay warm
  and continue helping.
"""


# ---------------------------------------------------------------------
# Scripted user inputs — simulates one full call with a barge-in
# ---------------------------------------------------------------------

# (label, transcript, is_interim_only).
# Interim transcripts test the InterruptionManager's threshold/grace path;
# finals commit to ConversationHistory and trigger a real LLM call.
SCRIPTED_USER_TURNS = [
    ("greet", "Hi", True),                                           # short interim, ignored
    ("greet", "Hi, are you open this weekend?", False),
    ("question", "Do you have a double room available Friday to Sunday?", False),
    ("ack", "okay", True),                                            # short interim, ignored
    ("question", "What's the rate, and is breakfast included in that?", False),
    ("interrupt", "actually wait, just give me the cheapest room", False),  # 6 words: real barge-in
    ("close", "perfect, please hold the booking, thanks", False),
]

# Mock LLM script — one reply per user FINAL transcript (5 of them, since
# the welcome is provided separately as config.first_message).
MOCK_LLM_REPLIES = [
    "Yes, we're open all weekend. Friday to Sunday works fine.",
    "Mm, let me check… yes, we have a double available those nights. Two adults?",
    "It's 480 cedis a night, breakfast included. Want me to hold it?",  # gets barged-in
    "Got it — cheapest room is 320 cedis a night, no breakfast. Same dates?",
    "Booked. I'll send a confirmation SMS shortly. Thanks for calling Adinkra.",
]


async def run_demo() -> None:
    print("=" * 60)
    print("  CYNEA VOICE ENGINE — FULL CALL DEMO")
    print("  Kwame @ Adinkra Hotel, Accra")
    print("=" * 60)

    # --- 1. Configure the agent --------------------------------------
    config = AgentConfig(
        name="kwame_adinkra",
        system_prompt=KWAME_DISCLOSED_PROMPT,
        stt_provider="whisper",       # not actually called in this demo
        llm_provider="mock",
        tts_provider="edge_tts",
        voice="en-GB-RyanNeural",
        speed=0.95,
        first_message=MOCK_LLM_REPLIES[0],
    )

    # --- 2. Wire the modules -----------------------------------------
    history = ConversationHistory()
    history.set_system_prompt(config.system_prompt)
    history.append_welcome(config.first_message)

    interruptions = InterruptionManager(word_threshold=3, grace_period_ms=700)

    metrics = MetricsTracker(rate_card=RateCard.default_africa())
    call = metrics.start_call(call_id="demo-call-001", agent=config.name)

    MockLLM.script(MOCK_LLM_REPLIES)
    llm = get_llm_provider(config.llm_provider)

    # TTS is best-effort; if edge-tts isn't installed, just log text.
    tts = None
    try:
        tts = get_tts_provider(config.tts_provider)
    except ValueError:
        print("[demo] edge-tts not registered; skipping audio synthesis.")

    out_dir = os.path.join(os.path.dirname(__file__), "_out")
    os.makedirs(out_dir, exist_ok=True)

    print(f"\nKwame: {config.first_message}")
    call.record_assistant_turn(
        text=config.first_message,
        llm_input_tokens=_estimate_tokens(history.for_llm()),
        llm_output_tokens=_estimate_tokens([{"content": config.first_message}]),
    )
    await _maybe_synth(tts, config, config.first_message, out_dir, turn=0)

    # --- 3. Walk the scripted call -----------------------------------
    turn_idx = 0
    for label, transcript, is_interim in SCRIPTED_USER_TURNS:
        await asyncio.sleep(0.05)  # simulate small inter-turn gap

        if is_interim:
            interruptions.on_interim_user_speech(transcript)
            triggered = interruptions.should_trigger_interruption(
                transcript, agent_speaking=True,
            )
            print(f"\n[{label}] interim: {transcript!r} -> "
                  f"{'INTERRUPT' if triggered else 'ignore'}")
            if triggered:
                interruptions.fire_interruption()
                history.pop_unheard()
                call.record_interruption()
            continue

        # Final transcript path
        interruptions.on_final_user_speech(transcript)

        # Mid-call barge-in: cancel any in-flight assistant audio first.
        if label == "interrupt":
            interruptions.fire_interruption()
            popped = history.pop_unheard()
            print(f"\n[{label}] BARGE-IN: dropped {len(popped)} unheard assistant message(s)")
            call.record_interruption()

        # Append user turn (merge if continuation).
        merged = history.merge_continuation(transcript)
        history.append_user(merged)
        call.record_user_turn(merged)
        print(f"\n[{label}] user: {merged}")

        # Allocate an outgoing sequence id — every chunk this turn must
        # be checked against InterruptionManager.is_valid().
        seq = interruptions.next_sequence_id()
        interruptions.on_agent_speech_started()

        reply = await llm.generate(history.for_llm(), system=config.system_prompt)

        # Imagine we now stream TTS audio in N chunks. Before each chunk
        # we'd check is_valid(seq); for this demo we simulate one chunk.
        if not interruptions.is_valid(seq):
            print(f"[{label}] (response cancelled before send)")
            continue

        history.append_assistant(reply)
        call.record_assistant_turn(
            text=reply,
            llm_input_tokens=_estimate_tokens(history.for_llm()),
            llm_output_tokens=_estimate_tokens([{"content": reply}]),
        )
        print(f"Kwame: {reply}")

        await _maybe_synth(tts, config, reply, out_dir, turn=turn_idx + 1)
        interruptions.on_agent_speech_ended()
        turn_idx += 1

    # --- 4. Close out -----------------------------------------------
    # We pretend the whole call was 42 s and the user paid for it.
    call.record_telephony_seconds(42.0)
    call.record_stt_duration(15.3)  # only the user-speaking portion
    call.set_outcome(containment=True, resolution=True)

    metrics.end_call(call)

    # Pull interruption stats from the manager into the call record.
    im_stats = interruptions.stats()
    call.notes = (
        f"interruptions={im_stats['interruption_count']}, "
        f"recovery_rate={im_stats['barge_in_recovery_rate']}"
    )

    json_path = os.path.join(out_dir, "calls.json")
    csv_path = os.path.join(out_dir, "calls.csv")
    metrics.export_json(json_path)
    metrics.export_csv(csv_path)

    print("\n" + "=" * 60)
    print("  CALL ENDED")
    print("=" * 60)
    print(f"Duration:        {call.duration_s:.2f}s")
    print(f"User turns:      {call.user_turns}")
    print(f"Assistant turns: {call.assistant_turns}")
    print(f"Interruptions:   {call.interruptions}")
    print(f"Sentiment:       {call.sentiment_score:+.2f}")
    print(f"Total cost:      {call.cost_total_cents:.3f} cents")
    print(f"  -> STT:         {call.cost_breakdown['stt_cents']:.3f} cents")
    print(f"  -> LLM:         {call.cost_breakdown['llm_cents']:.3f} cents")
    print(f"  -> TTS:         {call.cost_breakdown['tts_cents']:.3f} cents")
    print(f"  -> Telephony:   {call.cost_breakdown['telephony_cents']:.3f} cents")

    print("\nFleet summary:")
    for k, v in metrics.summary().items():
        print(f"  {k}: {v}")

    print(f"\nWrote {json_path}")
    print(f"Wrote {csv_path}")


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _estimate_tokens(messages: list) -> int:
    """Cheap token estimator (~4 chars per token). Used for the metrics
    rate card; replace with tiktoken in production."""
    chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
    return max(1, chars // 4)


async def _maybe_synth(tts, config: AgentConfig, text: str, out_dir: str, turn: int) -> None:
    """Synthesize audio if a TTS provider is wired; otherwise no-op."""
    if tts is None:
        return
    try:
        request = SynthesisRequest(text=text, voice=config.voice, speed=config.speed)
        audio = await tts.synthesize(request)
        if not audio:
            return
        path = os.path.join(out_dir, f"kwame_turn_{turn:02d}.mp3")
        with open(path, "wb") as f:
            f.write(audio)
        print(f"  [tts] wrote {path} ({len(audio)} bytes)")
    except Exception as exc:
        print(f"  [tts] skipped (no network or no edge-tts): {exc}")


if __name__ == "__main__":
    asyncio.run(run_demo())
