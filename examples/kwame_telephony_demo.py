# Cynea Voice Engine — Full Kwame Demo with Telephony
# Complete demonstration: Kwame answers a real phone call

import asyncio
from cynea import CyneaEngine, AgentConfig
from cynea_africa.persona.kwame import (
    KWAME_SYSTEM_PROMPT,
    KWAME_VOICE_CONFIG,
    KWAME_FIRST_MESSAGE
)
from cynea_africa.telephony import AfricasTalkingHandler, ATConfig, AfricanVoiceUtils


async def main():
    print("\\033[96m")  # Cyan
    print("=" * 60)
    print("   CYNEA VOICE ENGINE — KWAME WITH TELEPHONY")
    print("   Ghana Hotel Receptionist — Ready for Calls")
    print("=" * 60)
    print("\\033[0m")
    
    # Step 1: Configure the AI agent
    config = AgentConfig(
        name="kwame_hotel_ghana",
        system_prompt=KWAME_SYSTEM_PROMPT,
        stt_provider="whisper",
        llm_provider="anthropic",
        tts_provider="edge_tts",
        voice=KWAME_VOICE_CONFIG["voice"],
        speed=KWAME_VOICE_CONFIG["speed"],
        first_message=KWAME_FIRST_MESSAGE,
        interruption_enabled=True,
    )
    
    # Step 2: Create the engine
    engine = CyneaEngine(config)
    
    # Step 3: Configure telephony for Ghana
    at_config = ATConfig(
        username="your_africastalking_username",
        api_key="your_api_key",
        phone_number="+233XXXXXXXXX",
        country="GH"
    )
    
    telephony = AfricasTalkingHandler(at_config)
    
    # Step 4: Connect telephony to engine
    async def handle_incoming_audio(chunk):
        response = await engine.process_audio(chunk)
        if response:
            print(f"\\nKwame: {response}")
            # In production, send response audio to caller
            # audio_bytes = await engine._synthesize(response)
            # await telephony.send_audio(audio_bytes)
    
    telephony.on_audio(handle_incoming_audio)
    
    # Step 5: Print setup summary
    print(f"\\n?? Phone Number: {at_config.phone_number}")
    print(f"?? Country: Ghana")
    print(f"???  Voice: {KWAME_VOICE_CONFIG['voice']}")
    print(f"?? LLM: Claude (Anthropic)")
    print(f"?? STT: Whisper (local, free)")
    print(f"?? TTS: Edge TTS (free)")
    print(f"\\n{'='*60}")
    print("\\033[92m? Kwame is ready to receive calls!\\033[0m")
    print(f"\\n?? To connect a real phone number:")
    print(f"   1. Sign up at https://africastalking.com")
    print(f"   2. Get your API key")
    print(f"   3. Update ATConfig with your credentials")
    print(f"   4. Run this script")
    print(f"\\n?? Cost: FREE (Whisper + Edge TTS)")
    print(f"   Only pay for: Claude API + Africa's Talking minutes")


if __name__ == "__main__":
    asyncio.run(main())
