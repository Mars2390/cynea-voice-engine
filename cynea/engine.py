# Cynea Voice Engine — Core Orchestrator
# The heart of the voice agent pipeline

import asyncio
from typing import Optional, AsyncIterator
from cynea.models import (
    AgentConfig, Conversation, ConversationState,
    Transcription, SynthesisRequest, AudioChunk
)


class CyneaEngine:
    """Core orchestrator for voice conversations"""
    
    def __init__(self, config: AgentConfig):
        self.config = config
        self.conversation = Conversation()
        self.state = ConversationState.IDLE
        self._interrupted = False
    
    async def start(self):
        """Initialize the engine and return the first message"""
        self.state = ConversationState.SPEAKING
        return self.config.first_message
    
    async def process_audio(self, audio: AudioChunk) -> Optional[str]:
        """
        Process incoming audio through the full pipeline:
        Audio -> STT -> LLM -> TTS -> Response text
        """
        # Step 1: Transcribe audio to text
        transcription = await self._transcribe(audio)
        
        if not transcription or not transcription.text.strip():
            return None
        
        # Step 2: Add user turn to conversation
        self.conversation.add_turn("user", transcription.text)
        
        # Step 3: Get LLM response
        self.state = ConversationState.THINKING
        response_text = await self._generate_response(transcription.text)
        
        # Step 4: Add assistant turn
        self.conversation.add_turn("assistant", response_text)
        
        # Step 5: Generate speech
        self.state = ConversationState.SPEAKING
        return response_text
    
    async def _transcribe(self, audio: AudioChunk) -> Optional[Transcription]:
        """Transcribe audio using configured STT provider"""
        # Placeholder — will be replaced with actual STT provider
        from cynea.providers import get_stt_provider
        provider = get_stt_provider(self.config.stt_provider)
        return await provider.transcribe(audio)
    
    async def _generate_response(self, user_text: str) -> str:
        """Generate response using configured LLM provider"""
        from cynea.providers import get_llm_provider
        provider = get_llm_provider(self.config.llm_provider)
        messages = self.conversation.to_messages()
        system = self.config.system_prompt
        return await provider.generate(messages, system)
    
    async def _synthesize(self, text: str) -> bytes:
        """Convert text to speech using configured TTS provider"""
        from cynea.providers import get_tts_provider
        provider = get_tts_provider(self.config.tts_provider)
        request = SynthesisRequest(
            text=text,
            voice=self.config.voice,
            speed=self.config.speed
        )
        return await provider.synthesize(request)
    
    def interrupt(self):
        """Handle caller interruption"""
        self._interrupted = True
        self.state = ConversationState.INTERRUPTED
    
    def resume(self):
        """Resume after interruption"""
        self._interrupted = False
        self.state = ConversationState.IDLE
    
    def get_metrics(self) -> dict:
        """Get current call metrics"""
        return {
            "state": self.state.value,
            "turns": len(self.conversation.turns),
            "interrupted": self._interrupted,
        }
