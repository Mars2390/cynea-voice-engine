# Cynea Voice Engine � Provider Registry
# Pluggable providers for STT, LLM, and TTS

from typing import Protocol, Optional
from cynea.models import Transcription, SynthesisRequest, AudioChunk


class STTProvider(Protocol):
    """Protocol for speech-to-text providers"""
    async def transcribe(self, audio: AudioChunk) -> Optional[Transcription]:
        ...


class LLMProvider(Protocol):
    """Protocol for language model providers"""
    async def generate(self, messages: list, system: str = "") -> str:
        ...


class TTSProvider(Protocol):
    """Protocol for text-to-speech providers"""
    async def synthesize(self, request: SynthesisRequest) -> bytes:
        ...


# Provider registries
_stt_providers: dict = {}
_llm_providers: dict = {}
_tts_providers: dict = {}


def register_stt(name: str, provider_class):
    """Register a speech-to-text provider"""
    _stt_providers[name] = provider_class


def register_llm(name: str, provider_class):
    """Register a language model provider"""
    _llm_providers[name] = provider_class


def register_tts(name: str, provider_class):
    """Register a text-to-speech provider"""
    _tts_providers[name] = provider_class


def get_stt_provider(name: str):
    """Get a speech-to-text provider instance"""
    if name not in _stt_providers:
        raise ValueError(f"Unknown STT provider: {name}. Available: {list(_stt_providers.keys())}")
    return _stt_providers[name]()


def get_llm_provider(name: str):
    """Get a language model provider instance"""
    if name not in _llm_providers:
        raise ValueError(f"Unknown LLM provider: {name}. Available: {list(_llm_providers.keys())}")
    return _llm_providers[name]()


def get_tts_provider(name: str):
    """Get a text-to-speech provider instance"""
    if name not in _tts_providers:
        raise ValueError(f"Unknown TTS provider: {name}. Available: {list(_tts_providers.keys())}")
    return _tts_providers[name]()


# ----------------------------------------------------------------------
# Built-in mock LLM — lets examples and tests run with no API key.
# Register as "mock"; do not use in production.
# ----------------------------------------------------------------------

class MockLLM:
    """Deterministic LLM for TESTS ONLY.

    Registered so the suite and offline examples can run without an API
    key. It is never the default -- AgentConfig.llm_provider is "groq" --
    and selecting it logs a warning, because an agent wired to this in
    production answers every caller with a canned line.

    Replies with the next entry in `scripted_replies` (cycles when exhausted)
    or, if none provided, with a fixed acknowledgement. Tracks call count
    and last messages so tests can assert on them.
    """

    _scripted: list = []
    _index: int = 0
    last_messages: list = []
    call_count: int = 0

    @classmethod
    def script(cls, replies: list) -> None:
        cls._scripted = list(replies)
        cls._index = 0
        cls.call_count = 0

    def __init__(self):
        import logging
        logging.getLogger("cynea.providers").warning(
            "llm_provider='mock' selected. This is the test double: it "
            "returns canned text and never contacts a model. Use 'groq' "
            "for anything a real caller will hear."
        )

    async def generate(self, messages: list, system: str = "") -> str:
        type(self).last_messages = messages
        type(self).call_count += 1
        if self._scripted:
            reply = self._scripted[self._index % len(self._scripted)]
            type(self)._index += 1
            return reply
        return "Sure, let me help with that."


# ----------------------------------------------------------------------
# Auto-registration
#
# Each provider module registers itself at import time, so the only job
# here is to *import* them. That distinction matters: the ElevenLabs
# synthesiser always carried its own register_tts() call, but nothing
# imported the module, so the registry never saw it and
# get_tts_provider("elevenlabs") raised. Importing is the fix.
#
# Every import is guarded: a provider whose optional dependency is absent
# is simply unavailable, and asking for it by name gives a clear error
# listing what *is* registered.
# ----------------------------------------------------------------------

register_llm("mock", MockLLM)

# --- LLM ---------------------------------------------------------------
try:
    import cynea.llms  # noqa: F401  (registers "groq" and the "anthropic" alias)
except Exception as _exc:  # pragma: no cover - depends on optional httpx
    import logging
    logging.getLogger("cynea.providers").warning(
        "LLM adapters unavailable (%s). Only the 'mock' provider is registered.", _exc
    )

# --- STT ---------------------------------------------------------------
try:
    from cynea_africa.transcriber.whisper import WhisperTranscriber
    register_stt("whisper", WhisperTranscriber)
except ImportError:
    pass

try:
    from cynea_africa.transcriber.whisper import GroqWhisperTranscriber
    register_stt("groq_whisper", GroqWhisperTranscriber)
except ImportError:
    pass

try:
    import cynea_africa.transcriber.groq_stt  # noqa: F401  (registers "groq-stt")
except ImportError:
    pass

# --- TTS ---------------------------------------------------------------
try:
    from cynea_africa.synthesizer.edge_tts import EdgeTTSSynthesizer
    register_tts("edge_tts", EdgeTTSSynthesizer)
except ImportError:
    pass

try:
    from cynea_africa.synthesizer.elevenlabs_synthesizer import ElevenLabsSynthesizer
    register_tts("elevenlabs", ElevenLabsSynthesizer)
except ImportError:
    pass


def registered() -> dict:
    """Snapshot of what is actually wired up. Useful in tests and at boot."""
    return {
        "stt": sorted(_stt_providers),
        "llm": sorted(_llm_providers),
        "tts": sorted(_tts_providers),
    }
