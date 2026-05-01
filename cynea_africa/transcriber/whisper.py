# Cynea Africa — Whisper Transcriber
# Free local speech-to-text using OpenAI Whisper
# Runs on 8GB RAM with the "base" model (~1.5GB memory)

import asyncio
from typing import Optional
from cynea.models import Transcription, AudioChunk


class WhisperTranscriber:
    \"\"\"Local STT provider using OpenAI Whisper\"\"\"
    
    def __init__(self, model_size: str = "base"):
        self.model_size = model_size
        self._model = None
    
    def _load_model(self):
        \"\"\"Lazy-load the Whisper model\"\"\"
        if self._model is None:
            import whisper
            self._model = whisper.load_model(self.model_size)
        return self._model
    
    async def transcribe(self, audio: AudioChunk) -> Optional[Transcription]:
        \"\"\"Transcribe audio to text\"\"\"
        try:
            # Load model (first call will download it)
            model = await asyncio.to_thread(self._load_model)
            
            # Save audio to temporary file (Whisper reads from files)
            import tempfile
            import wave
            
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                with wave.open(f.name, 'wb') as wf:
                    wf.setnchannels(audio.channels)
                    wf.setsampwidth(2)
                    wf.setframerate(audio.sample_rate)
                    wf.writeframes(audio.data)
                temp_path = f.name
            
            # Transcribe
            result = await asyncio.to_thread(
                model.transcribe, temp_path, language="en"
            )
            
            # Clean up temp file
            import os
            os.unlink(temp_path)
            
            # Extract text and confidence
            text = result["text"].strip()
            confidence = result.get("confidence", 0.0)
            
            if not text:
                return None
            
            return Transcription(
                text=text,
                confidence=confidence,
                is_final=True,
                language=result.get("language", "en")
            )
            
        except Exception as e:
            print(f"Whisper transcription error: {e}")
            return None


# Register with Cynea provider system
try:
    from cynea.providers import register_stt
    register_stt("whisper", WhisperTranscriber)
except ImportError:
    pass
