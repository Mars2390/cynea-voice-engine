"""Cynea Voice Engine — core orchestrator.

CyneaEngine is a thin coordinator that wires:
  - cynea.conversation.ConversationHistory (history + barge-in trim)
  - cynea.interruption.InterruptionManager (sequence-id cancellation,
    threshold barge-in, grace period)
  - the pluggable provider registry (cynea.providers)

Public API is stable:
    engine = CyneaEngine(config)
    first  = await engine.start()
    reply  = await engine.process_audio(audio_chunk)
    engine.interrupt()
    engine.resume()
    metrics = engine.get_metrics()
"""

from __future__ import annotations

from typing import Optional

from cynea.conversation import ConversationHistory
from cynea.interruption import InterruptionManager
from cynea.models import (
    AgentConfig,
    AudioChunk,
    ConversationState,
    SynthesisRequest,
    Transcription,
)


class CyneaEngine:
    """Drives one voice conversation end-to-end."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self.history = ConversationHistory()
        self.interruption = InterruptionManager()
        self.state = ConversationState.IDLE

        if config.system_prompt:
            self.history.set_system_prompt(config.system_prompt)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> str:
        """Initialise the call and return the agent's first message."""
        self.state = ConversationState.SPEAKING
        first = self.config.first_message or ""
        if first:
            self.history.append_welcome(first)
            self.interruption.on_agent_speech_started()
        return first

    async def process_audio(self, audio: AudioChunk) -> Optional[str]:
        """Run one turn: STT -> barge-in check -> LLM -> assistant text.

        Returns the assistant reply text, or None if the turn was
        cancelled, the transcript was empty, or a provider failed.
        """
        # --- 1. STT ----------------------------------------------------
        self.state = ConversationState.LISTENING
        try:
            transcription = await self._transcribe(audio)
        except Exception as exc:
            print(f"[engine] STT error: {exc}")
            self.state = ConversationState.IDLE
            return None

        if not transcription or not transcription.text.strip():
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
            print(f"[engine] LLM error: {exc}")
            self.state = ConversationState.IDLE
            return None

        if not self.interruption.is_valid(sequence_id):
            # Caller barged in while the LLM was generating — drop the reply.
            return None

        self.history.add_assistant(response_text)
        self.interruption.on_agent_speech_started()
        self.state = ConversationState.SPEAKING
        return response_text

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
        try:
            from cynea.providers import get_tts_provider
            provider = get_tts_provider(self.config.tts_provider)
            request = SynthesisRequest(
                text=text, voice=self.config.voice, speed=self.config.speed,
            )
            return await provider.synthesize(request)
        except Exception as exc:
            print(f"[engine] TTS error: {exc}")
            return b""

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
