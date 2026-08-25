# Cynea Voice Engine
# AI Voice Agent Platform for African Businesses
# https://cynea.ai

# Load .env before anything reads os.getenv(). python-dotenv was already a
# declared dependency but nothing called it, so GROQ_API_KEY and
# ELEVENLABS_API_KEY sat in .env invisible to every provider that looked
# them up. Loading here means importing `cynea` is enough.
try:
    from dotenv import load_dotenv, find_dotenv

    load_dotenv(find_dotenv(usecwd=True), override=False)
except ImportError:  # pragma: no cover - dotenv is optional at runtime
    pass

from cynea.models import (
    AgentConfig,
    Conversation,
    ConversationState,
    AudioChunk,
    Transcription,
    SynthesisRequest,
)

from cynea.engine import (
    CyneaEngine,
    TurnResult,
    EngineError,
    STTError,
    LLMError,
    TTSError,
)
from cynea import providers

__version__ = "0.1.0"
__all__ = [
    "CyneaEngine",
    "TurnResult",
    "EngineError",
    "STTError",
    "LLMError",
    "TTSError",
    "AgentConfig",
    "Conversation",
    "ConversationState",
    "AudioChunk",
    "Transcription",
    "SynthesisRequest",
    "providers",
]
