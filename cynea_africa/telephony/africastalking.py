# Cynea Africa — Africa's Talking Telephony Handler
# Native integration with African phone networks
# Supports: Kenya, Nigeria, Ghana, South Africa, Uganda, Tanzania, Rwanda

import asyncio
import base64
import json
from typing import Optional, Callable
from dataclasses import dataclass
from cynea.models import AudioChunk


@dataclass
class ATConfig:
    \"\"\"Africa's Talking configuration\"\"\"
    username: str
    api_key: str
    phone_number: str
    country: str = "KE"  # KE, NG, GH, ZA, UG, TZ, RW


class AfricasTalkingHandler:
    \"\"\"
    Africa's Talking telephony integration.
    
    Handles:
    - Incoming voice calls via SIP
    - Audio streaming (bidirectional)
    - DTMF detection for USSD-like flows
    - Call state management
    
    Setup:
    1. Create account at https://africastalking.com
    2. Get API key from dashboard
    3. Register a phone number
    4. Configure SIP endpoint to point to this handler
    \"\"\"
    
    def __init__(self, config: ATConfig):
        self.config = config
        self._on_audio: Optional[Callable] = None
        self._on_dtmf: Optional[Callable] = None
        self._on_hangup: Optional[Callable] = None
        self._active = False
        self._call_sid: Optional[str] = None
    
    async def start(self):
        \"\"\"Initialize the telephony handler\"\"\"
        self._active = True
        
        # In production, this would:
        # 1. Register a SIP endpoint with Africa's Talking
        # 2. Start listening for incoming call webhooks
        # 3. Establish WebSocket connection for audio streaming
        
        print(f"[Africa's Talking] Handler started")
        print(f"  Country: {self.config.country}")
        print(f"  Number: {self.config.phone_number}")
        print(f"  Status: Ready for calls")
    
    async def stop(self):
        \"\"\"Shutdown the telephony handler\"\"\"
        self._active = False
        print(f"[Africa's Talking] Handler stopped")
    
    def on_audio(self, callback: Callable):
        \"\"\"Register callback for incoming audio chunks\"\"\"
        self._on_audio = callback
    
    def on_dtmf(self, callback: Callable):
        \"\"\"Register callback for DTMF digits (for USSD-like flows)\"\"\"
        self._on_dtmf = callback
    
    def on_hangup(self, callback: Callable):
        \"\"\"Register callback for call hangup\"\"\"
        self._on_hangup = callback
    
    async def send_audio(self, audio_data: bytes):
        \"\"\"Send audio to the caller\"\"\"
        if not self._active:
            return
        
        # In production, sends audio over the established SIP/WebSocket
        # For now, this is the interface contract
        pass
    
    async def send_dtmf(self, digits: str):
        \"\"\"Send DTMF tones to the caller\"\"\"
        if not self._active:
            return
        print(f"[DTMF Sent] {digits}")
    
    async def hangup(self):
        \"\"\"End the current call\"\"\"
        self._active = False
        self._call_sid = None
        if self._on_hangup:
            await self._on_hangup()
    
    async def process_incoming_call(self, webhook_data: dict) -> dict:
        \"\"\"
        Process an incoming call webhook from Africa's Talking.
        
        Returns the response that tells Africa's Talking how to handle the call.
        \"\"\"
        self._call_sid = webhook_data.get("callSessionState", {}).get("sessionId")
        caller_number = webhook_data.get("callSessionState", {}).get("callerNumber")
        
        print(f"[Incoming Call] From: {caller_number}")
        print(f"[Incoming Call] Session: {self._call_sid}")
        
        # Tell Africa's Talking to connect the call
        # In production, we return instructions to stream audio to our engine
        return {
            "action": "dial",
            "phoneNumber": self.config.phone_number,
        }
    
    async def process_audio_callback(self, audio_data: dict):
        \"\"\"Process incoming audio from the call\"\"\"
        if not self._active or not self._on_audio:
            return
        
        # Decode the audio (typically base64 encoded PCM)
        raw_audio = base64.b64decode(audio_data.get("payload", ""))
        
        # Create an AudioChunk for the engine
        chunk = AudioChunk(
            data=raw_audio,
            sample_rate=8000,  # Africa's Talking uses 8kHz
            channels=1,
        )
        
        await self._on_audio(chunk)
    
    async def process_dtmf_callback(self, dtmf_data: dict):
        \"\"\"Process DTMF input from the caller\"\"\"
        if not self._active or not self._on_dtmf:
            return
        
        digit = dtmf_data.get("dtmfDigit", "")
        await self._on_dtmf(digit)


# African voice-specific utilities
class AfricanVoiceUtils:
    \"\"\"Utilities for African telephony environments\"\"\"
    
    # Common African phone number prefixes
    COUNTRY_CODES = {
        "KE": "+254",
        "NG": "+234",
        "GH": "+233",
        "ZA": "+27",
        "UG": "+256",
        "TZ": "+255",
        "RW": "+250",
    }
    
    # Major carriers per country
    CARRIERS = {
        "KE": ["Safaricom", "Airtel Kenya", "Telkom Kenya"],
        "NG": ["MTN Nigeria", "Airtel Nigeria", "Globacom", "9mobile"],
        "GH": ["MTN Ghana", "Vodafone Ghana", "AirtelTigo Ghana"],
        "ZA": ["Vodacom", "MTN South Africa", "Cell C", "Telkom SA"],
        "UG": ["MTN Uganda", "Airtel Uganda"],
        "TZ": ["Vodacom Tanzania", "Airtel Tanzania", "Tigo Tanzania"],
        "RW": ["MTN Rwanda", "Airtel Rwanda"],
    }
    
    @classmethod
    def format_number(cls, number: str, country: str = "KE") -> str:
        \"\"\"Format a phone number to E.164 standard\"\"\"
        # Remove spaces, dashes, parentheses
        cleaned = number.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        
        # If starts with 0, replace with country code
        if cleaned.startswith("0"):
            cleaned = cls.COUNTRY_CODES.get(country, "+254") + cleaned[1:]
        
        # If no + prefix, add it
        if not cleaned.startswith("+"):
            cleaned = cls.COUNTRY_CODES.get(country, "+254") + cleaned
        
        return cleaned
    
    @classmethod
    def detect_country(cls, number: str) -> Optional[str]:
        \"\"\"Detect country from phone number prefix\"\"\"
        for code, prefix in cls.COUNTRY_CODES.items():
            if number.startswith(prefix):
                return code
        return None


# Register with Cynea
__all__ = [
    "AfricasTalkingHandler",
    "ATConfig",
    "AfricanVoiceUtils",
]
