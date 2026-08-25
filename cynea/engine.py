"""Cynea Voice Engine — core orchestrator.

CyneaEngine is a thin coordinator that wires:
  - cynea.conversation.ConversationHistory (history + barge-in trim)
  - cynea.interruption.InterruptionManager (sequence-id cancellation,
    threshold barge-in, grace period)
  - the pluggable provider registry (cynea.providers)

Public API:
    engine = CyneaEngine(config, on_error=page_someone)
    first  = await engine.start()          -> TurnResult (greeting + audio)
    turn   = await engine.process_audio(audio_chunk)   -> TurnResult | None
    engine.interrupt()
    engine.resume()
    metrics = engine.get_metrics()

Failure policy
--------------
Provider failures **raise**. They used to be caught, printed, and turned
into `None`, which made three very different situations look identical to
the caller: the model was unreachable, the transcript was empty, and the
turn was cancelled by barge-in. Only the first is an incident, and a phone
line that goes quiet without paging anyone is the worst failure mode this
system has.

So:
  - nothing said / cancelled by barge-in  -> returns None   (normal)
  - STT, LLM or TTS failed                -> raises         (incident)

Every raise also fires `on_error(stage, exc)` first, so a deployment can
page, increment a counter, or play a hold message before the exception
propagates.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from cynea.conversation import ConversationHistory
from cynea.interruption import InterruptionManager
from cynea.models import (
    AgentConfig,
    AudioChunk,
    ConversationState,
    SynthesisRequest,
    Transcription,
)

log = logging.getLogger("cynea.engine")


# ----------------------------------------------------------------------
# Errors — one per pipeline hop, so handlers can tell them apart
# ----------------------------------------------------------------------

class EngineError(RuntimeError):
    """Base class for a failed pipeline hop."""
    stage = "engine"


class STTError(EngineError):
    stage = "stt"


class LLMError(EngineError):
    stage = "llm"


class TTSError(EngineError):
    stage = "tts"


# ----------------------------------------------------------------------
# Turn result
# ----------------------------------------------------------------------

@dataclass
class TurnResult:
    """One agent turn: what it said, and the audio to play down the line.

    Truthy when there is text, and `str(result)` is that text, so code
    written against the old `-> str` return keeps reading naturally.
    """

    text: str
    audio: bytes = b""
    audio_format: str = "mp3"
    sequence_id: int = 0
    user_text: str = ""
    call_id: Optional[str] = None      # database row, when persistence is on

    def __bool__(self) -> bool:
        return bool(self.text)

    def __str__(self) -> str:
        return self.text

    @property
    def has_audio(self) -> bool:
        return bool(self.audio)


class CyneaEngine:
    """Drives one voice conversation end-to-end."""

    def __init__(
        self,
        config: AgentConfig,
        on_error: Optional[Callable[[str, Exception], None]] = None,
        *,
        synthesize: bool = True,
        agent_id: Optional[str] = None,
        caller_number: str = "unknown",
        persist: bool = True,
    ):
        """
        Args:
            config: the agent configuration.
            on_error: called as on_error(stage, exception) before the
                exception is re-raised. Wire this to Sentry, a pager, or a
                metric counter. Exceptions inside the callback are logged
                and suppressed so a broken alerter cannot mask the original
                fault.
            synthesize: set False to run the loop text-only (tests, chat
                transports, transcript replays) without paying for TTS.
            agent_id: the database row this call belongs to. Without it
                nothing is persisted — there is nowhere to attach the call.
            caller_number: the number on the other end, supplied by the
                telephony layer. Defaults to "unknown" rather than a
                stand-in like "test", so a call recorded without one is
                visibly missing it instead of looking like a real number.
            persist: set False to keep a call entirely out of the database
                (previews, evaluation runs, tests).
        """
        self.config = config
        self.history = ConversationHistory()
        self.interruption = InterruptionManager()
        self.state = ConversationState.IDLE
        self.on_error = on_error
        self.synthesize = synthesize

        # --- persistence -------------------------------------------------
        self.agent_id = agent_id
        self.caller_number = caller_number
        self.persist = persist and bool(agent_id)
        self.call_id: Optional[str] = None      # set on the first saved turn
        self._closed = False

        # metrics.CallRecord already computes running sentiment and a full
        # cost breakdown from a RateCard, so the engine records into it
        # rather than growing its own arithmetic.
        self._metrics = None
        if self.persist:
            try:
                from cynea_africa.dashboard.metrics import CallRecord, RateCard
                self._metrics = CallRecord(
                    call_id=str(uuid.uuid4()),
                    agent=config.persona or config.name,
                )
                self._rate_card = RateCard.default_africa()
            except Exception as exc:      # metrics must never break a call
                log.warning("metrics unavailable, cost/sentiment will be 0: %s", exc)
                self.persist = False

        if config.system_prompt:
            self.history.set_system_prompt(config.system_prompt)

    # ------------------------------------------------------------------
    # Failure handling
    # ------------------------------------------------------------------

    def _fail(self, error_cls, message: str, exc: Exception):
        """Log loudly, alert, and raise. Never returns."""
        log.error("[%s] %s: %s", error_cls.stage, message, exc, exc_info=True)
        if self.on_error:
            try:
                self.on_error(error_cls.stage, exc)
            except Exception:  # a broken alerter must not hide the real fault
                log.exception("on_error callback itself raised; original error follows")
        self.state = ConversationState.IDLE
        raise error_cls(f"{message}: {exc}") from exc

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> TurnResult:
        """Initialise the call and return the agent's opening turn."""
        self.state = ConversationState.SPEAKING
        first = self.config.first_message or ""
        if not first:
            return TurnResult(text="")

        self.history.append_welcome(first)
        self.interruption.on_agent_speech_started()
        audio = await self._synthesize(first) if self.synthesize else b""
        return TurnResult(text=first, audio=audio, sequence_id=0)

    async def process_audio(self, audio: AudioChunk) -> Optional[TurnResult]:
        """Run one turn: STT -> barge-in check -> LLM -> TTS.

        Returns:
            TurnResult with the reply text and the synthesised audio, or
            None when the caller said nothing intelligible or the turn was
            cancelled by barge-in.

        Raises:
            STTError, LLMError, TTSError — a provider actually failed.
        """
        # --- 1. STT ----------------------------------------------------
        self.state = ConversationState.LISTENING
        try:
            transcription = await self._transcribe(audio)
        except Exception as exc:
            self._fail(STTError, "transcription failed", exc)

        if not transcription or not transcription.text.strip():
            # Genuinely nothing said. Not an error; let the caller keep talking.
            self.state = ConversationState.IDLE
            return None

        # --- 2. Barge-in check + commit user turn ----------------------
        text = transcription.text.strip()
        agent_was_speaking = self.state == ConversationState.SPEAKING
        self.interruption.on_interim_user_speech(text)
        if self.interruption.should_trigger_interruption(text, agent_speaking=agent_was_speaking):
            self.interrupt()
        self.interruption.on_final_user_speech(text)
        self.history.add_user(text)

        # --- 3. LLM, gated by sequence id ------------------------------
        sequence_id = self.interruption.next_sequence_id()
        self.state = ConversationState.THINKING
        try:
            response_text = await self._generate_response()
        except Exception as exc:
            self._fail(LLMError, "generation failed", exc)

        if not self.interruption.is_valid(sequence_id):
            # Caller barged in while the LLM was generating — drop the reply
            # rather than talking over them. Not an error.
            log.info("turn %s superseded by barge-in; reply discarded", sequence_id)
            return None

        self.history.add_assistant(response_text)

        # --- 4. TTS ----------------------------------------------------
        audio_bytes = b""
        if self.synthesize and response_text:
            audio_bytes = await self._synthesize(response_text)

        self.interruption.on_agent_speech_started()
        self.state = ConversationState.SPEAKING

        # --- 5. persist ------------------------------------------------
        # One row per CALL, updated in place — not one row per turn. The
        # calls table is call-shaped (duration, status, full transcript),
        # so a row per exchange would break every aggregate the dashboard
        # computes. save_call() inserts on the first turn and updates the
        # same row afterwards, which also lets the console show a call
        # while it is still running.
        if self.persist:
            self._record_turn(user_text=text, reply=response_text)
            self.save_call()

        return TurnResult(
            text=response_text,
            audio=audio_bytes,
            audio_format=getattr(self.config, "audio_format", "mp3"),
            sequence_id=sequence_id,
            user_text=text,
            call_id=self.call_id,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _record_turn(self, user_text: str, reply: str) -> None:
        """Feed one exchange into the metrics record."""
        if not self._metrics:
            return
        try:
            self._metrics.record_user_turn(user_text)
            # Token counts are approximated at 4 chars/token until the
            # adapter surfaces real usage from the provider response.
            self._metrics.record_assistant_turn(
                text=reply,
                llm_input_tokens=sum(len(m.get("content") or "")
                                     for m in self.history.for_llm()) // 4,
                llm_output_tokens=len(reply) // 4,
            )
            if self.interruption.stats()["interruption_count"]:
                self._metrics.interruptions = self.interruption.stats()["interruption_count"]
        except Exception as exc:
            log.warning("could not record metrics for this turn: %s", exc)

    def _infer_status(self) -> str:
        """Map conversation state onto the three stored outcomes.

        Deliberately conservative: a call is only 'resolved' once it has
        been closed with end_call(). While it is still running it stays
        'abandoned', so a crash or a dropped line is never silently
        recorded as a success.
        """
        if not self._closed:
            return "abandoned"
        if self._metrics and self._metrics.containment is False:
            return "escalated"
        return "resolved"

    def save_call(self) -> Optional[str]:
        """Write this call to the database. Returns the call id.

        Inserts on first use, updates the same row after. Never raises:
        a storage outage must not drop a live phone call, so failures are
        logged and reported through on_error instead.
        """
        if not self.persist:
            return None

        try:
            from cynea import db

            if self._metrics:
                self._metrics.finalize(self._rate_card)
                duration = int(self._metrics.duration_s or 0)
                sentiment = round(float(self._metrics.sentiment_score), 3)
                cost = int(round(self._metrics.cost_total_cents))
            else:
                duration, sentiment, cost = 0, None, 0

            transcript = self.transcript_text()
            status = self._infer_status()

            if self.call_id is None:
                call = db.log_call(
                    agent_id=self.agent_id,
                    caller_number=self.caller_number,
                    duration=duration,
                    transcript=transcript,
                    sentiment=sentiment,
                    cost=cost,
                    status=status,
                )
                self.call_id = call.id
                log.info("call %s opened for agent %s", self.call_id, self.agent_id)
            else:
                with db.session_scope() as s:
                    row = s.get(db.Call, self.call_id)
                    if row is not None:
                        row.duration_s = duration
                        row.transcript = transcript
                        row.sentiment_score = sentiment
                        row.cost_cents = cost
                        row.status = status
            return self.call_id

        except Exception as exc:
            log.error("could not save call for agent %s: %s",
                      self.agent_id, exc, exc_info=True)
            if self.on_error:
                try:
                    self.on_error("db", exc)
                except Exception:
                    log.exception("on_error callback raised while reporting a db fault")
            return self.call_id

    # Stored transcripts are "<outcome summary>\n\n<dialogue>". The console's
    # history table reads line one as the outcome, so the engine and the
    # seed script must agree on that shape or engine-written calls show a
    # greeting where an outcome belongs.
    _OUTCOME_LABELS = {
        "resolved": "Handled by agent",
        "escalated": "Escalated to human",
        "abandoned": "Call ended early",
    }

    def dialogue_text(self) -> str:
        """The conversation as speaker-labelled plain text, no header."""
        lines = []
        for message in self.history.messages:
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            who = "Caller" if role == "user" else (self.config.persona or "Agent").title()
            lines.append(f"{who}: {(message.get('content') or '').strip()}")
        return "\n".join(lines)

    def transcript_text(self) -> str:
        """Outcome summary followed by the dialogue."""
        outcome = self._OUTCOME_LABELS.get(self._infer_status(), "Call recorded")
        return f"{outcome}\n\n{self.dialogue_text()}"

    def end_call(self, *, escalated: bool = False, notes: str = "") -> Optional[str]:
        """Close the call and write the final row.

        Call this when the line drops. Until it runs the call stays
        'abandoned', which is the honest default for a call still in
        progress or one that died unexpectedly.
        """
        self._closed = True
        if self._metrics:
            self._metrics.set_outcome(
                containment=not escalated,
                resolution=not escalated,
                notes=notes or None,
            )
        self.state = ConversationState.IDLE
        return self.save_call()

    # ------------------------------------------------------------------
    # Provider hops — kept private so callers don't depend on registry shape
    # ------------------------------------------------------------------

    async def _transcribe(self, audio: AudioChunk) -> Optional[Transcription]:
        from cynea.providers import get_stt_provider
        provider = get_stt_provider(self.config.stt_provider)
        return await provider.transcribe(audio)

    async def _generate_response(self) -> str:
        from cynea.providers import get_llm_provider
        provider = get_llm_provider(self.config.llm_provider)
        return await provider.generate(self.history.for_llm(), self.config.system_prompt)

    async def _synthesize(self, text: str) -> bytes:
        """Render `text` to audio bytes. Raises TTSError on failure."""
        try:
            from cynea.providers import get_tts_provider
            provider = get_tts_provider(self.config.tts_provider)
            request = SynthesisRequest(
                text=text, voice=self.config.voice, speed=self.config.speed,
            )
            return await provider.synthesize(request)
        except Exception as exc:
            self._fail(TTSError, "synthesis failed", exc)

    # ------------------------------------------------------------------
    # External controls + introspection
    # ------------------------------------------------------------------

    def interrupt(self) -> None:
        """Cancel any in-flight assistant turn and trim unheard messages."""
        self.interruption.fire_interruption()
        self.history.pop_unheard()
        self.interruption.on_agent_speech_ended()
        self.state = ConversationState.INTERRUPTED

    def resume(self) -> None:
        """Return to idle so the next user turn can be processed."""
        self.state = ConversationState.IDLE

    def get_metrics(self) -> dict:
        stats = self.interruption.stats()
        return {
            "state": self.state.value,
            "turns": self.history.turn_count,
            "interrupted": stats["interruption_count"] > 0,
            "interruption_count": stats["interruption_count"],
        }
