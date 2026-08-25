# Cynea Voice Engine

**AI voice agents for African businesses.** Human-like agents that answer phone
calls, handle local accents and code-switching, and connect to African telephony.

Built in Nairobi.

---

## Status

Honest summary, because the gap between "written" and "wired up" matters:

| Area | State |
|---|---|
| Conversation core (barge-in, history, turn-taking) | **Working**, 152 tests |
| LLM → speech pipeline | **Working** end to end |
| Personas (Kwame, Amina, Kofi, Maya) | **Working** |
| Database layer | **Working** (Neon Postgres / SQLite) |
| HTTP API server | **Working** — auth, agents, calls, dashboard |
| Auth (bcrypt + sessions) | **Working** |
| Console reading live data | **Working** — real login, empty states, no sample data |
| Telephony (Africa's Talking) | **Interface only** — no audio transport |
| Billing, multi-tenancy beyond per-user scoping | **Not built** |

A full audit with a 92-item register and a four-phase roadmap is in
[GAP_ANALYSIS.md](GAP_ANALYSIS.md).

---

## Quick start

```bash
git clone https://github.com/Mars2390/cynea-voice-engine.git
cd cynea-voice-engine

python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env        # add GROQ_API_KEY (free: console.groq.com/keys)

pytest tests/ -q            # 152 tests, ~24s, no API key or Postgres needed
```

Run the whole stack:

```bash
python -m cynea.migrate                       # create tables
python -m cynea.seed --email you@cynea.ai     # provision the 4 agents
uvicorn cynea.api:app --port 8000

# then open signin.html and sign in with that account
```

The seed provisions the four personas as agents against an account you name,
and nothing else — no demo login, no sample calls, no invented metrics. Every
figure the console shows afterwards is one the system actually produced; with
an empty database it shows empty states, not placeholder numbers.

Talk to an agent:

```python
import asyncio
from cynea.agent_loader import AgentLoader
from cynea.models import AudioChunk

async def main():
    engine = AgentLoader().load_from_dict({
        "agent_name":  "front-desk",
        "persona":     "kwame",          # kwame | amina | kofi | maya
        "client_name": "Adinkra Hotel",  # substituted into the prompt
        "llm_provider": "groq",          # "mock" for offline tests
    })

    greeting = await engine.start()
    print(greeting.text)            # "Adinkra Hotel, this is Kwame..."
    print(len(greeting.audio))      # synthesised MP3 bytes, ready to play

    turn = await engine.process_audio(AudioChunk(data=pcm, sample_rate=16000))
    if turn:                        # None means silence or barge-in, not failure
        print(turn.text, len(turn.audio))

asyncio.run(main())
```

`load_from_file("config.json")` takes the same shape from disk. Only
`agent_name` and `persona` are required.

Check what is actually wired up:

```bash
python -c "import cynea; print(cynea.providers.registered())"
# {'stt': ['groq_whisper', 'whisper'],
#  'llm': ['anthropic', 'groq', 'mock'],
#  'tts': ['edge_tts', 'elevenlabs']}
```

---

## Architecture

```
                   ┌─────────────────────────────────────────┐
   caller ────────▶│  TELEPHONY   Africa's Talking / SIP      │  ⚠ interface only
                   │              8 kHz μ-law, bidirectional  │
                   └──────────────────┬──────────────────────┘
                                      │ AudioChunk
                                      ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  CyneaEngine                                    cynea/engine.py  │
   │                                                                  │
   │   ┌────────────┐   ┌───────────────┐   ┌──────────────────────┐  │
   │   │  History   │   │ Interruption  │   │  Provider registry   │  │
   │   │            │   │   Manager     │   │                      │  │
   │   │ • tool-call│   │ • sequence-id │   │  STT → LLM → TTS     │  │
   │   │   sanitise │   │   cancellation│   │  swappable by name   │  │
   │   │ • barge-in │   │ • 700ms grace │   │                      │  │
   │   │   trim     │   │ • 800ms back- │   │                      │  │
   │   │            │   │   channel     │   │                      │  │
   │   └────────────┘   └───────────────┘   └──────────────────────┘  │
   └────────┬──────────────────┬──────────────────────┬──────────────┘
            │                  │                      │
            ▼                  ▼                      ▼
     ┌─────────────┐    ┌─────────────┐        ┌─────────────┐
     │     STT     │    │     LLM     │        │     TTS     │
     │             │    │             │        │             │
     │ whisper     │    │ groq        │        │ edge_tts    │
     │  (local)    │    │  gpt-oss-20b│        │  9 voices   │
     │ groq_whisper│    │ mock        │        │ elevenlabs  │
     │  (hosted)   │    │  (tests)    │        │  (premium)  │
     └─────────────┘    └─────────────┘        └─────────────┘
                                      │
                                      ▼  TurnResult(text, audio)
                              back down the line

   ┌──────────────────────────────────────────────────────────────────┐
   │  PERSISTENCE                                        cynea/db.py  │
   │  users → agents → { calls, prompt_versions }   Neon Postgres     │
   └──────────────────────────────────────────────────────────────────┘
```

**One turn:** audio in → STT → barge-in check → LLM (gated by sequence id) →
TTS → `TurnResult(text, audio)`.

**Failure policy.** Provider failures *raise*; they are never swallowed into a
silent `None`. Silence and barge-in return `None` because they are normal.
A dead phone line that pages nobody is the worst failure this system has, so
the two cases are kept distinguishable:

```python
engine = CyneaEngine(config, on_error=lambda stage, exc: sentry.capture(exc))
```

---

## Personas

Each lives in `cynea_africa/persona/` and ships a system prompt, a voice config,
a first message, a speech-pattern spec, and a conversation flowchart.

| Persona | Role | Market | Voice |
|---|---|---|---|
| **Kwame** | Hotel receptionist | Accra, Ghana | `en-GB-RyanNeural` |
| **Amina** | Bank support | Nairobi, Kenya | `en-GB-SoniaNeural` |
| **Kofi** | Restaurant orders | Ghana | `en-GB-RyanNeural` |
| **Maya** | Bookings & scheduling | Pan-African | `en-US-AriaNeural` |

All four disclose that they are AI **once**, on the opening turn, then converse
normally — the pattern Klarna, BoA Erica and KCB use, and what California
SB 1001, EU AI Act Art. 50 and Kenya ODPC 2024 require. They do not repeat it as
a disclaimer; that is itself a robotic tell.

Adding one is a file plus one registration block in `cynea/agent_loader.py`.

---

## API reference

### `CyneaEngine`

```python
CyneaEngine(config: AgentConfig, on_error=None, *, synthesize=True)
```

| Method | Returns | Notes |
|---|---|---|
| `await start()` | `TurnResult` | Greeting text + audio |
| `await process_audio(chunk)` | `TurnResult \| None` | `None` = silence or barge-in |
| `interrupt()` | `None` | Cancel in-flight turn, trim unheard history |
| `resume()` | `None` | Back to idle |
| `get_metrics()` | `dict` | state, turns, interruption count |

`TurnResult` carries `.text`, `.audio`, `.audio_format`, `.user_text`,
`.has_audio`. It is truthy when there is text and `str()`s to `.text`.

Raises `STTError`, `LLMError`, `TTSError` (all `EngineError`).

### Providers

```python
from cynea import providers
providers.registered()                     # what is wired up
providers.register_llm("name", MyLLM)      # add your own
```

Any class with the right `async` method works — `transcribe(audio)`,
`generate(messages, system)`, or `synthesize(request)`.

### HTTP API

```
POST   /auth/register              GET    /agents/{id}
POST   /auth/login                 PUT    /agents/{id}
GET    /auth/me                    DELETE /agents/{id}
POST   /agents                     POST   /agents/{id}/prompt
GET    /agents                     GET    /agents/{id}/prompts

POST   /calls                      GET    /dashboard/stats
GET    /calls?agent_id=            GET    /dashboard/queue
GET    /calls/{id}                 GET    /dashboard/bootstrap
GET    /health
```

Bearer tokens from `/auth/login`. Every agent and call route is scoped to
the authenticated user — ownership is re-checked on each lookup, so another
workspace's rows 404 even with a valid id. Interactive docs at `/docs`.

```bash
curl -s localhost:8000/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"you@cynea.ai","password":"..."}'
```

### Persisting calls

```python
engine = CyneaEngine(config, agent_id=agent.id, caller_number="+233240004417")
await engine.start()
await engine.process_audio(chunk)     # row created, then updated per turn
engine.end_call()                     # marks resolved and writes the final row
```

One row per **call**, not per turn — the row is created on the first turn and
updated in place, so the console can show a call while it is still running.
Sentiment and cost come from `metrics.CallRecord`, which already computes
both. A call stays `abandoned` until `end_call()`, so a dropped line is never
silently recorded as a success.

### Database

```python
from cynea import db

db.init_db()
user  = db.create_user("ama@example.com", password_hash)
agent = db.create_agent(user.id, "Front Desk", "kwame", {"voice": "en-GB-RyanNeural"})
db.log_call(agent.id, "+233240004417", 79, transcript="...",
            sentiment=0.62, cost=5, status="resolved")   # cost is integer CENTS
db.save_prompt_version(agent.id, "# Kwame\n...")         # auto-numbered
```

`users → agents → {calls, prompt_versions}`, cascading deletes, UUID keys.
Money is integer cents, never a float.

---

## Configuration

`.env` is loaded automatically on `import cynea`. See `.env.example`.

| Variable | Required | Purpose |
|---|---|---|
| `GROQ_API_KEY` | **yes** | LLM, and hosted STT fallback |
| `DATABASE_URL` | for persistence | Neon Postgres, or `sqlite:///cynea.db` |
| `ELEVENLABS_API_KEY` | no | Premium voices; `edge_tts` is free |
| `GROQ_MODEL` | no | Default `openai/gpt-oss-20b` |

> **Model note.** The brief specified `llama-3.3-70b-versatile`, but this
> account exposes no Llama models at all — that name returns 404. The default
> is `openai/gpt-oss-20b`. It is a *reasoning* model, so `max_tokens` must stay
> generous: at 40 it spends the whole budget thinking and returns an empty
> string. The adapter raises a clear error rather than letting the agent go
> silent. List your own models with
> `GET https://api.groq.com/openai/v1/models`.

---

## Speech-to-text

Two interchangeable paths:

```bash
pip install openai-whisper        # local, free, private, needs ffmpeg + ~1.5 GB
# or just set GROQ_API_KEY        # hosted, nothing to install
```

`best_available_stt()` picks whichever is present. Local Whisper keeps call
audio on your own infrastructure, which matters for Kenya DPA and POPIA — it is
a real competitive advantage, not just a cost saving.

---

## Deployment

**Docker**

```bash
docker build -t cynea .                             # ~400 MB, hosted STT
docker build --build-arg WITH_WHISPER=1 -t cynea .  # ~3 GB, local STT
docker run --env-file .env -p 8000:8000 cynea
```

**Host.** The engine needs long-lived WebSockets for carrier audio, so it
**cannot run on Vercel** (`vercel.json` here is static-site config for the
marketing pages only). Use Fly.io, Railway, or Render.

**Database**

```bash
python -m cynea.migrate --check    # connectivity + what exists
python -m cynea.migrate            # create missing tables
```

**CI.** `.github/workflows/ci.yml` runs the suite on 3.11 and 3.13, asserts
every provider and persona registers, and builds the image.

---

## Project layout

```
cynea/                    engine, providers, persistence
  engine.py               orchestrator, TurnResult, failure policy
  interruption.py         barge-in, sequence-id cancellation, grace period
  conversation.py         history, tool-call sanitisation
  providers.py            STT/LLM/TTS registry
  llms/groq_llm.py        LLM adapter (streaming + non-streaming)
  db.py                   SQLAlchemy models and CRUD
  migrate.py              schema creation
  seed.py                 demo workspace (user, 4 agents, sample calls)
  auth.py                 bcrypt hashing + signed session tokens
  api.py                  FastAPI routes
  dashboard_data.py       read models for the console
  agent_loader.py         persona registry, JSON config loading

cynea_africa/             Africa-specific modules
  persona/                kwame, amina, kofi, maya
  synthesizer/            edge_tts, elevenlabs
  transcriber/            whisper (local) + groq_whisper (hosted)
  telephony/              Africa's Talking  ⚠ interface only
  dashboard/              metrics + legacy HTML generator

tests/                    152 tests
*.html                    marketing site and operations console
```

---

## Contributing

```bash
pytest tests/ -q          # must pass before you push
```

New code needs tests. `interruption.py` and `conversation.py` especially —
they are stateful, reachable from every live call, and were entirely untested
until recently.

---

## Licence

Proprietary — © 2026 Cynea AI.
