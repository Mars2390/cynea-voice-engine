# cynea-voice — the live-call socket on Cloudflare

A call sits silent for most of its length, so the browser call
(`live-call.html` → `assets/live-call.js`) lives where a silent socket costs
nothing: a hibernating Durable Object. Nothing else moves: the site and the API
stay on Vercel, and `cynea/voice_ws.py` stays as the local FastAPI fallback.

```
browser ──wss──▶ Worker (src/main.py: Default) ──▶ VoiceSession Durable Object "line-N"
                  origin check, picks a free line     Hibernation API, one call per line
                                                       Groq Whisper → Cerebras → Edge TTS
```

| File | What it is |
|---|---|
| `src/main.py` | Worker entry + `VoiceSession` Durable Object (Hibernation API) |
| `wrangler.toml` | bindings, SQLite migration, vars, build step |
| `pyproject.toml` | Python dependencies, vendored by `pywrangler` |
| `sync_shared.py` | build step: copies `cynea/` + `cynea_africa/` modules into `src/` (gitignored) |
| `check_idle.py` | proves a deployed call survives a long silence |

## Deploy (Windows PowerShell; the same commands work in bash)

```powershell
# 1. Tooling — once. uv must be on PATH (the official installer does that);
#    Node 18+ is also needed, because pywrangler drives wrangler through npx.
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv tool install workers-py

# 2. Log in — opens a browser. Account de7a1526080d248448165ee50fa5ee56 is
#    already pinned in wrangler.toml.
cd voice-worker
pywrangler login
pywrangler whoami

# 3. Deploy. Prints the URL: https://cynea-voice.<your-subdomain>.workers.dev
pywrangler deploy

# 4. Secrets — each prompts for the value; nothing goes in a file.
pywrangler secret put CEREBRAS_API_KEY      # required: the model
pywrangler secret put GROQ_API_KEY          # required: Whisper transcription only
pywrangler secret put ELEVENLABS_API_KEY    # optional — unused by this socket
pywrangler secret put DATABASE_URL          # optional — unused by this socket
pywrangler secret put SESSION_SECRET        # optional — unused by this socket

# 5. Test
curl https://cynea-voice.<your-subdomain>.workers.dev/voice/health
uv run --with websockets python check_idle.py wss://cynea-voice.<your-subdomain>.workers.dev 120
pywrangler tail                             # live logs while you call it from the site
```

`/voice/health` uses each key against its provider and synthesizes one word,
at most once a minute. It returns `200` with `"ok": true` when a turn would
work and `503` when it would not, with `"failed"` naming the secret or service
and `"reason"` saying what to do, for example:

```json
{"ok": false, "failed": ["GROQ_API_KEY"],
 "reason": "GROQ_API_KEY is set but Groq rejected it (HTTP 401): the key is invalid, revoked or expired. Create a new key, then run: pywrangler secret put GROQ_API_KEY"}
```

Callers never see that detail. They get a plain apology, and the cause goes to
`pywrangler tail`.

## Limits on the public demo

| Var | Default | What it does |
|---|---|---|
| `VOICE_DEMO_MAX_CONCURRENT` | 6 | Lines, so calls at once |
| `VOICE_DEMO_MAX_CALL_S` | 600 | Longest call |
| `VOICE_DEMO_MAX_TURNS` | 40 | Most turns in a call |
| `VOICE_DEMO_IDLE_S` | 180 | Silence that ends a call, so a forgotten tab frees its line |
| `VOICE_DEMO_CALLS_PER_HOUR` | 10 | Calls one address may start in an hour (IPv6 counts per /64); `0` turns it off |

The address is hashed and only used to name a counter; the counter clears
itself an hour after the last call.

## The model: Cerebras

Every reply comes from Cerebras (`https://api.cerebras.ai/v1`,
OpenAI-compatible), in `CerebrasLLM` in `src/main.py`:

| Turn | Model | Set by |
|---|---|---|
| everything else | `gpt-oss-120b` | `CEREBRAS_MODEL` |
| caller said ≤ 3 words ("yes", "thank you") | `qwen-3.8-27b`, always with `reasoning_effort: "none"` | `CEREBRAS_FAST_MODEL`, `CEREBRAS_FAST_MAX_WORDS` (`0` = off) |

Both see the full persona prompt and history. The client is plain httpx,
not the openai SDK: the SDK adds 2,708 modules and a 124 s cold start in local
dev, to wrap the same POST.

Transcription is still Whisper on Groq — Cerebras serves language models
only — so a call needs a working `GROQ_API_KEY` as well. The FastAPI
fallback (`cynea/voice_ws.py`) still uses Groq for both.

Then point the browser at it — one line in `assets/live-call.js`
(`socketURL()`, line 66):

```js
base = local ? 'ws://127.0.0.1:8000' : 'wss://cynea-voice.<your-subdomain>.workers.dev';
```

`ALLOWED_ORIGINS` in `wrangler.toml` already admits `https://cynea.ai` and
`https://www.cynea.ai`. Add a Vercel preview hostname there if you want
previews to place calls; anything else is refused with 403.

## Run it locally

```powershell
cd voice-worker
"CEREBRAS_API_KEY=csk-...", "GROQ_API_KEY=gsk_..." | Out-File -Encoding ascii .dev.vars   # gitignored
pywrangler dev
```

Then open `live-call.html` from a local server and, in the console,
`localStorage.setItem('cynea_api', 'http://127.0.0.1:8787')`.

`pywrangler dev` watches `../cynea` and `../cynea_africa` and reloads when
anything in them changes — including `__pycache__` written by a pytest run.
A reload drops every open call with 1006. Don't run the repo's tests while
timing a long call locally.

## Is the socket staying open, and is it hibernating?

`check_idle.py` opens a call, waits for the greeting, then sends nothing at
all — no data, no protocol pings — for the number of seconds you give it,
and then hangs up with a control frame. `PASS: open for 332s, then woke and
hung up cleanly (1000 'hangup')` means the socket outlived five minutes of
silence *and* the Durable Object woke from hibernation to answer it.

To see that the silence was not billed: Cloudflare dashboard → Workers &
Pages → cynea-voice → Durable Objects → VoiceSession → Metrics. Over that
run, wall-clock duration should cover the greeting and the hangup — a few
seconds — not the five idle minutes in between.

If a deployed call ever drops during silence while `check_idle.py` passed
locally, add a heartbeat to the browser: `setInterval(() => ws.send('ping'), 30000)`.
The Durable Object answers `ping` with `pong` without waking up
(`setWebSocketAutoResponse`), so it costs nothing, and live-call.js already
ignores frames that are not JSON.

## Limits worth knowing

- **Calls end at 10 minutes** (`VOICE_DEMO_MAX_CALL_S`), enforced by a
  Durable Object alarm even on a silent call — an abandoned tab cannot hold
  a line. The FastAPI socket defaulted to 5 minutes but only checked when
  the next utterance arrived.
- **Six concurrent calls** (`VOICE_DEMO_MAX_CONCURRENT`, which is also the
  number of `line-N` objects). The seventh caller hears "All 6 demo lines
  are in use" and is closed with 1013.
- **40 turns, 30-second utterances**, as before.
- **Edge TTS has no contract.** It is Microsoft's free browser endpoint. Microsoft has
  moved its `Sec-MS-GEC` token to a v2 scheme, and on 2026-11-03 edge-tts
  7.2.8 was refused with HTTP 403; `pyproject.toml` pins
  `edge-tts>=7.3.1`, which was checked and is answered. If synthesis
  starts failing with 403 again, bump edge-tts and redeploy.
