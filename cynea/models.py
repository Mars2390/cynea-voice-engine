# Cynea Voice Engine � Data Models
# Core data structures for the voice agent pipeline

from dataclasses import dataclass, field
from typing import Optional, Literal
from enum import Enum


class ProviderType(str, Enum):
    STT = "stt"
    LLM = "llm"
    TTS = "tts"
    TELEPHONY = "telephony"


class ConversationState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    ENDED = "ended"


@dataclass
class AudioChunk:
    """Raw audio data from a call.

    encoding values:
      "pcm16"  — 16-bit signed little-endian linear PCM (default)
      "mulaw"  — 8-bit ITU-T G.711 μ-law (Twilio, most SIP carriers)
      "alaw"   — 8-bit ITU-T G.711 A-law (some European/African carriers)
    """
    data: bytes
    sample_rate: int = 16000
    channels: int = 1
    timestamp: float = 0.0
    encoding: str = "pcm16"


@dataclass
class Transcription:
    """Speech-to-text result"""
    text: str
    confidence: float = 0.0
    is_final: bool = False
    language: Optional[str] = None


@dataclass
class SynthesisRequest:
    """Text-to-speech request"""
    text: str
    voice: str = "en-GB-RyanNeural"
    speed: float = 0.95


@dataclass
class ConversationTurn:
    """A single turn in a conversation"""
    speaker: Literal["user", "assistant"]
    text: str
    timestamp: float = 0.0


@dataclass
class Conversation:
    """Full conversation history"""
    turns: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    
    def add_turn(self, speaker: str, text: str):
        self.turns.append(ConversationTurn(speaker=speaker, text=text))
    
    def to_messages(self):
        """Convert to LLM-compatible message format"""
        messages = []
        for turn in self.turns:
            role = "user" if turn.speaker == "user" else "assistant"
            messages.append({"role": role, "content": turn.text})
        return messages
    
    def last_user_text(self) -> Optional[str]:
        for turn in reversed(self.turns):
            if turn.speaker == "user":
                return turn.text
        return None


@dataclass
class AgentConfig:
    """Configuration for a voice agent.

    The fields below the divider are optional metadata used by the
    loader, dashboard, and call-routing layers — the engine itself does
    not require them to operate.
    """
    name: str = "cynea_agent"
    system_prompt: str = ""
    stt_provider: str = "whisper"
    llm_provider: str = "anthropic"
    tts_provider: str = "edge_tts"
    voice: str = "en-GB-RyanNeural"
    speed: float = 0.95
    first_message: str = "Hello?"
    interruption_enabled: bool = True
    backchanneling_enabled: bool = True
    # ── optional metadata ────────────────────────────────────────────
    persona: Optional[str] = None
    client_name: Optional[str] = None
    location: Optional[str] = None
    max_call_duration: int = 600
    escalation_number: Optional[str] = None


@dataclass
class CallMetrics:
    """Operational metrics for a call"""
    call_duration: float = 0.0
    user_turns: int = 0
    assistant_turns: int = 0
    interruptions: int = 0
    sentiment_score: float = 0.0
    containment: bool = True
    resolution: bool = True
