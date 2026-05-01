# Cynea Africa — Edge TTS Synthesizer
# Free text-to-speech using Microsoft Edge TTS
# Natural voices, zero API cost, works offline

import asyncio
import tempfile
import os
from cynea.models import SynthesisRequest


class EdgeTTSSynthesizer:
    \"\"\"Free TTS provider using Edge TTS\"\"\"
    
    # Available natural voices
    VOICES = {
        "en-GB-RyanNeural": "British male, warm",
        "en-GB-SoniaNeural": "British female, warm",
        "en-GB-LibbyNeural": "British female, soft",
        "en-US-EricNeural": "American male, professional",
        "en-US-AriaNeural": "American female, natural",
        "en-ZA-LeahNeural": "South African female, warm",
        "en-ZA-LukeNeural": "South African male, deep",
        "en-NG-EzinneNeural": "Nigerian female, natural",
        "en-NG-AbeoNeural": "Nigerian male, deep",
    }
    
    def __init__(self, voice: str = "en-GB-RyanNeural"):
        self.voice = voice
    
    async def synthesize(self, request: SynthesisRequest) -> bytes:
        \"\"\"Convert text to speech and return audio bytes\"\"\"
        try:
            import edge_tts
            
            voice = request.voice or self.voice
            
            # Apply speed adjustment
            rate = f"+{int((request.speed - 1.0) * 100)}%" if request.speed > 1.0 else f"{int((request.speed - 1.0) * 100)}%"
            
            # Generate speech
            communicate = edge_tts.Communicate(
                text=request.text,
                voice=voice,
                rate=rate
            )
            
            # Save to temp file
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                temp_path = f.name
            
            await communicate.save(temp_path)
            
            # Read audio data
            with open(temp_path, "rb") as f:
                audio_data = f.read()
            
            # Clean up
            os.unlink(temp_path)
            
            return audio_data
            
        except ImportError:
            raise ImportError(
                "edge-tts is required. Install with: pip install edge-tts"
            )
        except Exception as e:
            print(f"Edge TTS synthesis error: {e}")
            return b""


# Register with Cynea provider system
try:
    from cynea.providers import register_tts
    register_tts("edge_tts", EdgeTTSSynthesizer)
except ImportError:
    pass
