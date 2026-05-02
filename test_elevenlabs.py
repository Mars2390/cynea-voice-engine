import asyncio
from cynea_africa.synthesizer.elevenlabs_synthesizer import ElevenLabsSynthesizer

async def main():
    synth = ElevenLabsSynthesizer()
    health = await synth.health_check()
    print("Health check:", health)

asyncio.run(main())
