# Cynea Voice Engine

<p align="center">
  <img src="https://raw.githubusercontent.com/Mars2390/cynea-voice-engine/main/assets/cynea-logo.png" alt="Cynea AI" width="200"/>
</p>

<p align="center">
  <strong>AI Voice Agents Built for African Businesses</strong>
</p>

<p align="center">
  <a href="https://cynea.ai">Website</a> ·
  <a href="https://docs.cynea.ai">Documentation</a> ·
  <a href="https://dashboard.cynea.ai">Dashboard</a>
</p>

---

## Overview

Cynea Voice Engine is an open-source platform for building, deploying, and managing AI-powered voice agents. Built from the ground up for African markets with native support for local telephony providers, code-switching, and operations-focused dashboards.

## Why Cynea?

- **Africa-First**: Native integration with Africa's Talking, MTN, Safaricom, and local carriers
- **Natural Conversations**: Human-like turn-taking, interruption handling, and persona crafting
- **Code-Switching**: Handles English-Swahili, English-Twi, and other African language mixing
- **Operations Dashboard**: Cost-per-call analytics, sentiment tracking, agent performance
- **Agency Multitenancy**: Manage multiple client agents from a single console
- **Offline-Ready**: Local STT/TTS fallback for intermittent connectivity

## Features

### Voice Orchestration
- Real-time STT ? LLM ? TTS pipeline
- Provider abstraction: swap Deepgram, Whisper, Azure, Google STT
- Multi-LLM support: Claude, GPT, Gemini, Llama, Mistral
- 15+ TTS voices including African-accented English

### Conversation Craft
- Persona engine with customizable agent personalities
- Disfluency injection for natural speech patterns
- Latency masking during API calls
- Sentiment-triggered human escalation
- Contextual memory across conversation turns

### Telephony
- Africa's Talking integration
- Twilio support
- Generic SIP trunking
- DTMF handling for USSD-like flows

### Operations
- Real-time call monitoring
- Cost-per-resolved-call metrics
- Agent containment rate tracking
- Sentiment trend analysis
- Call recording and transcription

## Architecture
+-------------------------------------------------+
¦ TELEPHONY LAYER ¦
¦ Africa's Talking · Twilio · SIP · WebRTC ¦
+-------------------------------------------------+
¦
?
+-------------------------------------------------+
¦ SPEECH-TO-TEXT (STT) ¦
¦ Deepgram · Whisper · Azure · Google ¦
+-------------------------------------------------+
¦
?
+-------------------------------------------------+
¦ AI BRAIN (LLM) ¦
¦ Claude · GPT · Gemini · Llama · Mistral ¦
+-------------------------------------------------+
¦
?
+-------------------------------------------------+
¦ TEXT-TO-SPEECH (TTS) ¦
¦ ElevenLabs · Edge TTS · Azure · Polly ¦
+-------------------------------------------------+
¦
?
+-------------------------------------------------+
¦ CYNE ORCHESTRATOR ¦
¦ TaskManager · InterruptionManager · Craft ¦
+-------------------------------------------------+
¦
?
+-------------------------------------------------+
¦ OPERATIONS DASHBOARD ¦
¦ Analytics · Monitoring · Billing ¦
+-------------------------------------------------+

text

## Quick Start

### Prerequisites
- Python 3.10+
- 8GB RAM minimum
- Internet connection (for API-based providers)

### Installation

\\\ash
git clone https://github.com/Mars2390/cynea-voice-engine.git
cd cynea-voice-engine
pip install -r requirements.txt
\\\

### Run Your First Agent

\\\python
from cynea.assistant import Assistant
from cynea.models import Transcriber, Synthesizer, LlmAgent

# Configure your agent
assistant = Assistant(name="kwame_agent")

# Add a conversation pipeline
assistant.add_task(
    task_type="conversation",
    transcriber=Transcriber(provider="deepgram", model="nova-2"),
    llm_agent=LlmAgent(provider="anthropic", model="claude-sonnet-4-20250514"),
    synthesizer=Synthesizer(provider="elevenlabs", voice="George")
)

# Run the agent
async for chunk in assistant.execute():
    print(chunk)
\\\

## African Telephony Setup

\\\python
from cynea_africa.telephony import AfricasTalkingHandler

# Configure Africa's Talking
handler = AfricasTalkingHandler(
    username="your_username",
    api_key="your_api_key",
    phone_number="+254700000000"
)

# Connect to Cynea orchestrator
assistant.connect_telephony(handler)
\\\

## Persona Library

Cynea includes pre-built personas for African markets:

| Persona | Industry | Language |
|---------|----------|----------|
| Kwame | Hotel/Hospitality | Ghanaian English |
| Amina | Customer Service | Kenyan English |
| Chidi | Banking/Finance | Nigerian English |
| Thabo | Technical Support | South African English |

## Documentation

Full documentation at [docs.cynea.ai](https://docs.cynea.ai)

## Contributing

We welcome contributions. See [CONTRIBUTING.md](CONTRIBUTING.md)

## License

MIT License - see [LICENSE](LICENSE) for details

## Built by Cynea AI

[Cynea AI](https://cynea.ai) is building the voice AI infrastructure for African businesses.

---
<p align="center">
  <strong>Made with ?? in Kenya</strong>
</p>
