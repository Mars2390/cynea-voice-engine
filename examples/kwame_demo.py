# Cynea Voice Engine — Kwame Demo
# Working demonstration of the hotel receptionist voice agent

import asyncio
from cynea import CyneaEngine, AgentConfig
from cynea_africa.persona.kwame import (
    KWAME_SYSTEM_PROMPT,
    KWAME_VOICE_CONFIG,
    KWAME_FIRST_MESSAGE
)
from cynea_africa.theme import CYNE_COLORS


async def main():
    print(f"\\033[96m")  # Cyan text
    print("=" * 50)
    print("   CYNEA VOICE ENGINE — KWAME DEMO")
    print("   Hotel Receptionist Voice Agent")
    print("=" * 50)
    print(f"\\033[0m")  # Reset
    
    # Configure Kwame
    config = AgentConfig(
        name="kwame_hotel_agent",
        system_prompt=KWAME_SYSTEM_PROMPT,
        stt_provider="whisper",
        llm_provider="anthropic",
        tts_provider="edge_tts",
        voice=KWAME_VOICE_CONFIG["voice"],
        speed=KWAME_VOICE_CONFIG["speed"],
        first_message=KWAME_FIRST_MESSAGE,
        interruption_enabled=True,
    )
    
    # Create engine
    engine = CyneaEngine(config)
    
    # Start the agent
    first_message = await engine.start()
    print(f"\\nKwame: {first_message}")
    print(f"\\n{'='*50}")
    print("Agent is ready to receive calls.")
    print("=" * 50)
    
    # Print metrics
    metrics = engine.get_metrics()
    print(f"\\nMetrics: {metrics}")
    
    print(f"\\n\\033[92m? Kwame demo initialized successfully!\\033[0m")
    print(f"\\nTo connect to a real phone line:")
    print("  1. Set up Africa's Talking or Twilio")
    print("  2. Route calls to this engine")
    print("  3. Kwame will answer naturally")


if __name__ == "__main__":
    asyncio.run(main())
