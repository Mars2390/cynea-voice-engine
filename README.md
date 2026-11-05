# Cynea Voice Engine

**AI voice agents for African businesses.** Human-like agents that answer phone
calls, handle local accents and code-switching, and connect to African telephony.

Built in Nairobi.

---

## Status

Honest summary, because the gap between "written" and "wired up" matters:

| Area | State |
|---|---|
| Conversation core (history, turn-taking) | **Working** |
| Barge-in | **Tested in isolation**; the browser call does not use it |
| Browser call | **Working** on a Cloudflare Durable Object, see [voice-worker/](voice-worker/README.md) |
| LLM → speech pipeline | **Working** end to end |
| Personas (Peter, Amina, Kofi, Maya) | **Working** |
| Database layer | **Working** (Neon Postgres / SQLite) |
| HTTP API server | **Working** — auth, agents, calls, dashboard, API keys, webhooks |
| Auth (bcrypt + sessions) | **Working** — email verification, password reset, sign-out, rate limits |
| Console reading live data | **Working** — real login, empty states, no sample data |
| Telephony (Twilio) | **Test webhook only** — answers inbound calls with a fixed message; no agent on the line |
| Telephony (Africa's Talking) | **Interface only** — no audio transport |
| Billing, multi-tenancy beyond per-user scoping | **Not built** |

---

## Quick start

```bash
git clone https://github.com/Mars2390/cynea-voice-engine.git
cd cynea-voice-engine

python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env        # add GROQ_API_KEY (free: console.groq.com/keys)

python -m pytest tests/ -q  # about 3 minutes, no API key or Postgres needed
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
    print(greeting.text)            # "Adinkra Hotel, this is Peter..."
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
| **Peter** (`kwame`) | Hotel receptionist | Accra, Ghana | `en-GB-RyanNeural` |
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
GET    /auth/config                GET    /agents/{id}
POST   /auth/register              PUT    /agents/{id}
GET    /auth/verify?token=         DELETE /agents/{id}
POST   /auth/resend-verification   POST   /agents/{id}/prompt
POST   /auth/login                 GET    /agents/{id}/prompts
GET    /auth/me
POST   /agents                     POST   /calls/{id}/recording
GET    /agents                     GET    /calls/{id}/recording

POST   /calls                      GET    /dashboard/stats
GET    /calls?agent_id=            GET    /dashboard/queue
GET    /calls/{id}                 GET    /dashboard/bootstrap
GET    /health
```

Bearer tokens from `/auth/login`. Every agent and call route is scoped to
the authenticated user — ownership is re-checked on each lookup, so another
workspace's rows 404 even with a valid id. Interactive docs at `/docs` in
development; with `ENV=production` there are no docs and `/openapi.json`
needs a bearer token.

**Sign-up.** `signup.html` posts to `/auth/register`, which creates an
unverified account and emails a link (24-hour, single-use; only its sha256
is stored). `/auth/login` answers 403 "Please verify your email first."
until the link is followed. Registration is limited to 5 attempts per IP
per hour (counted in the database, so every serverless instance shares
it), and guarded by Cloudflare Turnstile when its keys are set.

**Recordings.** `POST /calls/{id}/recording` (multipart, field `file`, plus
optional `duration_s`) stores the call's audio in Cloudflare R2, or on local
disk when R2 is not configured. `GET /calls/{id}/recording` returns a link
that expires in 10 minutes, or 404 "No recording available". Recordings
older than `RECORDING_RETENTION_DAYS` are removed by
`python tools/purge_recordings.py` — run it on a schedule. Uploads through
Vercel are capped by its 4.5 MB request-body limit (about 4–5 minutes of
128 kbps MP3).

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
db.save_prompt_version(agent.id, "# Peter\n...")         # auto-numbered
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
| `SESSION_SECRET` | **yes, deployed** | Signs sessions and playback links; random per process if unset |
| `ELEVENLABS_API_KEY` | no | Premium voices; `edge_tts` is free |
| `GROQ_MODEL` | no | Default `openai/gpt-oss-20b` |

**On Vercel (the API)** — set these in Project → Settings → Environment Variables:

| Variable | Required | Purpose |
|---|---|---|
| `ENV` | **yes** | `production` — turns off `/docs` and `/redoc`, puts `/openapi.json` behind a bearer token |
| `DATABASE_URL`, `SESSION_SECRET` | **yes** | as above |
| `EMAIL_API_KEY` | for real sign-ups | Resend API key; without it the verification link is only logged |
| `EMAIL_FROM` | with `EMAIL_API_KEY` | e.g. `Cynea <hello@cynea.ai>` — a domain verified in Resend |
| `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY` | recommended | Cloudflare Turnstile on sign-up; skipped (with a warning) when unset |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | for recordings | Cloudflare R2; without them recordings land in `/tmp` and do not survive the instance |
| `RECORDING_RETENTION_DAYS` | no | Default `30` |
| `SITE_URL` | no | Where `signin.html` lives, if not the API's own origin |
| `REGISTER_RATE_LIMIT` | no | Sign-up attempts per IP per hour, default `5` |

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
python -m cynea.migrate            # create missing tables, add missing columns
```

`init_db()` also adds columns a model has gained to tables that already
exist (nullable ones only), so an upgrade is the same command. Accounts that
predate email verification are marked verified when that column first
appears.

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

tests/                    run on SQLite; test_postgres.py needs TEST_POSTGRES_URL
*.html                    marketing site and operations console
```

---

## Contributing

```bash
python -m pytest tests/ -q     # must pass before you push
```

CI runs this on Python 3.11 and 3.13, then runs `tests/test_postgres.py` against a
real `postgres:16`. To run that yourself, point `TEST_POSTGRES_URL` at a throwaway
database: it drops every table. What differs between SQLite and Postgres, and why
each difference has a test, is written at the top of that file.

`tests/test_worker.py` runs the Cloudflare socket's handlers with the Workers
runtime stubbed. It cannot catch runtime behaviour such as hibernation; check that
with `pywrangler dev` and a browser.

New code needs tests. `interruption.py` and `conversation.py` especially —
they are stateful, reachable from every live call, and were entirely untested
until recently.

---

## Licence

Proprietary — © 2026 Cynea AI.
