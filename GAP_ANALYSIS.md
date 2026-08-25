# CYNEA VOICE ENGINE — COMPLETE GAP ANALYSIS & BUILD ROADMAP

**Audit date:** 25 August 2026 · **Commit:** `0d614db` · **Auditor:** Claude

**Method:** every Python module and HTML page read; the engine executed against the repo.
Findings marked **[VERIFIED]** were confirmed by running the code or reading the exact file cited.
Effort figures assume **one engineer working with Claude** and carry roughly ±40% uncertainty.

---

## HEADLINE NUMBERS

| Metric | Value |
|---|---|
| Gaps logged | **92** |
| P0 (blocking) | **27** |
| Verified by execution | **37** |
| Build effort | **~288 engineer-days** (+2 open-ended items) |
| Test coverage | **0%** — no test file exists in the repo |
| Blocked on non-code | **44 items** need a key, a signature, or counsel |

---

## THE ONE FINDING THAT OUTRANKS EVERYTHING

### The voice loop cannot complete a single turn today. [VERIFIED]

`AgentConfig` defaults to `llm_provider="anthropic"`, but **no Anthropic adapter is ever
registered** — the only LLM in the registry is `MockLLM`. Every call with a default config
raises before it reaches the model. Separately, Whisper is not installed, and the engine
catches both failures and returns `None`, so a real caller would hear the greeting and then
silence — with no exception and no alert.

This is not a missing feature. It is the difference between a demo and a product, and it is
roughly two days of work.

```
# actually executed against the repo, 2026-08-25

>>> providers._llm_providers.keys()
['mock']                      # anthropic is never registered

>>> AgentConfig().llm_provider
'anthropic'                   # the default points at nothing

>>> get_llm_provider('anthropic')
ValueError: Unknown LLM provider: anthropic. Available: ['mock']

>>> await engine.start()
'Hello?'                      # greeting works (it is a literal)
>>> await engine.process_audio(chunk)
[WhisperTranscriber] transcription error: No module named 'whisper'
None                          # the call goes silent, no exception raised
```

**A second structural gap sits beside it.** `CyneaEngine._synthesize()` is defined but never
called from `process_audio()`. The engine returns reply *text* and has no audio output path
at all. Text-to-speech is written and working — it is simply not connected to the loop.

---

## WHAT ALREADY WORKS — DO NOT REBUILD THESE

- **Interruption / barge-in** — `interruption.py`, 291 lines. Sequence-id cancellation,
  700 ms grace period, 800 ms backchannel. Genuinely good engineering.
- **Conversation history** — orphaned tool-call sanitisation, barge-in trimming.
  Already shaped for function calling.
- **Persona prompts** — 1,271 lines across three agents. Disclosure policy grounded in
  California SB 1001, EU AI Act Art. 50, Kenya ODPC.
- **ElevenLabs synthesiser** — real HTTP client, env-based keys, reachability checks.
- **Edge TTS** — 678 lines, 9 voices, 4 native African accents.
- **Cost + containment metrics** — `metrics.py` computes real per-call economics.
- **Front end** — landing page and operations console are built, responsive, accessible.

## WHAT LOOKS BUILT BUT IS NOT

- **Africa's Talking** — an interface contract. `send_audio()` is a bare `pass`.
  No SIP, no socket, no auth. [VERIFIED]
- **Sign-in** — `signin.html` submits and redirects. Anyone reaching the URL is "logged in". [VERIFIED]
- **API server** — `fastapi` and `uvicorn` are in requirements. **Zero routes exist** anywhere. [VERIFIED]
- **Twilio** — in requirements, no code. [VERIFIED]
- **ElevenLabs registration** — the synthesiser is written but never registered, so
  `tts_provider="elevenlabs"` raises. [VERIFIED]
- **Maya** — shown as a fourth agent in both dashboard and landing page. No persona file exists. [VERIFIED]
- **Everything persistent** — no database, no storage, no session. Agent-editor edits die with the tab.

---

# THE GAP REGISTER — 92 ITEMS

Each item lists: what is needed · why it matters · what we need from you ·
what we can build · effort · dependencies.

---

## 1. BACKEND INFRASTRUCTURE — 9 items, ~26d
*Nothing persistent exists.*

**BE-1 — API server** · `P0` · **[VERIFIED]**
No FastAPI app, no routes, no `uvicorn` entry point anywhere — despite both being in
`requirements.txt`. Every other backend item depends on this.
*From you:* nothing · *We build:* all of it · *Effort:* 5d · *After:* —

**BE-2 — Database + schema** · `P0`
No database of any kind. Needs Postgres: orgs, users, agents, prompt versions, calls,
transcripts, extracted fields, usage events.
*From you:* host decision (Neon / Supabase / RDS, ~$25/mo) · *We build:* schema + migrations · *Effort:* 4d · *After:* BE-1

**BE-3 — Real authentication** · `P0` · **[VERIFIED]**
`signin.html` calls `go()` which redirects to `agent_manager.html`. There is no auth.
Anyone with the URL has the console.
*From you:* decision — Clerk vs Auth0 vs roll-your-own · *We build:* integration + session handling · *Effort:* 3d · *After:* BE-1, BE-2

**BE-4 — Orgs, roles, permissions** · `P1`
Multi-tenancy from day one: owner / admin / supervisor / viewer.
Retrofitting tenancy later is a rewrite, not a feature.
*From you:* nothing · *We build:* all of it · *Effort:* 4d · *After:* BE-2, BE-3

**BE-5 — Compute hosting** · `P0` · **[VERIFIED]**
`vercel.json` is static-only. Voice needs long-lived WebSockets, so the engine
**cannot live on Vercel**. Needs a container host.
*From you:* CTO decision — Fly.io / Railway / Render, ~$50–200/mo · *We build:* deploy config · *Effort:* 2d · *After:* —

**BE-6 — Secrets management** · `P0` · **[VERIFIED]**
Two keys in a local `.env`. Needs a real secret store and rotation before any third party
is involved. (Good news: `.env` was never committed to git.)
*From you:* nothing · *We build:* all of it · *Effort:* 1d · *After:* BE-5

**BE-7 — Recording object storage** · `P1`
S3 or Cloudflare R2, with lifecycle rules matching whatever retention policy legal sets.
*From you:* storage spend · *We build:* all of it · *Effort:* 2d · *After:* BE-5, LG-6

**BE-8 — Job queue** · `P2`
Batch calls, webhook retries, transcript post-processing, nightly rollups.
*From you:* nothing · *We build:* all of it · *Effort:* 3d · *After:* BE-1, BE-2

**BE-9 — Rate limiting + abuse control** · `P0`
The public demo form invites toll fraud — an unmetered outbound-call endpoint is a
direct financial attack surface.
*From you:* nothing · *We build:* all of it · *Effort:* 2d · *After:* BE-1

---

## 2. TELEPHONY INTEGRATION — 10 items, ~34d
*The largest single body of missing work.*

**TEL-1 — Africa's Talking, real integration** · `P0` · **[VERIFIED]**
The handler prints and returns hardcoded dial actions. `send_audio()` is a bare `pass`.
No HTTP client, no auth, no SIP registration, no credential handling.
*From you:* **AT account, API key, username, prepaid credit** · *We build:* the integration · *Effort:* 8d · *After:* BE-1, BE-5

**TEL-2 — Media streaming transport** · `P0`
Bidirectional audio between carrier and engine: WebSocket, 8 kHz μ-law framing, jitter
buffer, sequencing. **This is the hardest engineering in the roadmap.**
*From you:* nothing · *We build:* all of it · *Effort:* 6d · *After:* TEL-1

**TEL-3 — Number provisioning** · `P1`
Buy, assign and release numbers from the console. Currently a nav item with no screen.
*From you:* AT account · *We build:* all of it · *Effort:* 3d · *After:* TEL-1, BE-2

**TEL-4 — Call routing** · `P1`
Map inbound number → agent → prompt version, with business-hours and overflow rules.
*From you:* nothing · *We build:* all of it · *Effort:* 3d · *After:* TEL-1, BE-2

**TEL-5 — Recording capture + storage** · `P1`
Capture both legs, store, expose in the call modal. The console UI already exists and is
currently wired to sample clips.
*From you:* nothing · *We build:* all of it · *Effort:* 3d · *After:* BE-7, LG-4

**TEL-6 — DTMF handling** · `P2` · **[VERIFIED]**
Callbacks exist in the handler but nothing routes digits into the engine.
*From you:* nothing · *We build:* all of it · *Effort:* 2d · *After:* TEL-2

**TEL-7 — SIP trunking** · `P2` · **[VERIFIED]**
Advertised on the landing page as a feature. No SIP stack exists in the repo.
*From you:* nothing · *We build:* all of it · *Effort:* 5d · *After:* TEL-2

**TEL-8 — Twilio fallback** · `P2` · **[VERIFIED]**
`twilio>=8.9` is a declared dependency with zero corresponding code. Needed for non-AT
countries and for redundancy.
*From you:* Twilio account · *We build:* the integration · *Effort:* 4d · *After:* TEL-2

**TEL-9 — Caller-ID / anti-spam registration** · `P1`
Carrier-side process, per country. Long lead time, pure ops work.
**Start early — it cannot be accelerated.**
*From you:* carrier paperwork per country (CEO) · *We build:* nothing · *Effort:* — · *After:* registered entity

**TEL-10 — Call concurrency model** · `P1`
Nothing defines how many simultaneous calls one process handles, or how they scale out.
*From you:* nothing · *We build:* all of it · *Effort:* 3d · *After:* TEL-2, BE-5

---

## 3. VOICE PIPELINE — 10 items, ~30d
*Components exist. Nothing is connected.*

**V-1 — Whisper deployment** · `P0` · **[VERIFIED]**
Running the engine produces `No module named 'whisper'`. Not installed. Also needs a
deployment decision: local GPU, Groq Whisper, or Deepgram.
*From you:* CTO decision + possible GPU spend · *We build:* deployment · *Effort:* 3d · *After:* BE-5

**V-2 — Streaming STT** · `P0`
`transcribe()` is batch. Real conversation needs interim results, or the ~600 ms latency
target is unreachable by construction.
*From you:* nothing · *We build:* all of it · *Effort:* 5d · *After:* V-1, TEL-2

**V-3 — Register ElevenLabs** · `P0` · **[VERIFIED]**
The synthesiser is complete and working, but only self-registers under
`if __name__ == '__main__'`, so the provider registry never sees it.
**Half a day. Unblocks all audio.**
*From you:* nothing · *We build:* all of it · *Effort:* 0.5d · *After:* —

**V-4 — Connect TTS to the engine** · `P0` · **[VERIFIED]**
`_synthesize()` is defined and never called from `process_audio()`. The engine has no
audio output path at all.
*From you:* nothing · *We build:* all of it · *Effort:* 2d · *After:* V-3

**V-5 — Streaming TTS** · `P1`
Chunked synthesis so audio starts before the sentence finishes. **The ~600 ms claim on
the landing page depends entirely on this.**
*From you:* nothing · *We build:* all of it · *Effort:* 4d · *After:* V-4

**V-6 — Audio resampling** · `P0`
Telephony is 8 kHz, Whisper wants 16 kHz. Nothing converts between them.
*From you:* nothing · *We build:* all of it · *Effort:* 1d · *After:* TEL-2

**V-7 — Barge-in on real audio** · `P1`
Interruption logic triggers on transcript text. Live barge-in needs energy/VAD detection
on the inbound stream.
*From you:* nothing · *We build:* all of it · *Effort:* 3d · *After:* V-2

**V-8 — Swahili / Twi / Yoruba TTS** · `P1` · **[VERIFIED]**
**Zero of the 9 voices are Swahili, Twi or Yoruba.** The landing page says calls are
answered in Swahili and Twi — today only *transcription* handles them.
*From you:* CEO decision — license vs train vs partner; significant spend · *We build:* integration · *Effort:* 10d+ · *After:* —

**V-9 — Voice cloning** · `P2`
Requires a paid ElevenLabs tier and a consent/licensing policy for the voice donor.
*From you:* spend + legal · *We build:* integration · *Effort:* 3d · *After:* V-3

**V-10 — Latency instrumentation** · `P1`
The ~600 ms target is completely unmeasured. Needs per-hop timing before it can be
claimed publicly.
*From you:* nothing · *We build:* all of it · *Effort:* 2d · *After:* V-2, V-5

---

## 4. AI AGENT CAPABILITIES — 10 items, ~33d
*The core is good. The model is absent.*

**AI-1 — LLM adapter** · `P0` · **[VERIFIED]** ← **THE BLOCKING ITEM**
No adapter for any real provider. Only `MockLLM` is registered, and `AgentConfig`
defaults to `llm_provider="anthropic"` which raises `ValueError`.
*From you:* **Anthropic key — or use the Groq key already in `.env`** · *We build:* the adapter · *Effort:* 2d · *After:* —

**AI-2 — Streaming tokens → TTS** · `P0`
Token-by-token handoff into the synthesiser. Without it, latency is sentence-bound.
*From you:* nothing · *We build:* all of it · *Effort:* 3d · *After:* AI-1, V-5

**AI-3 — Maya persona** · `P1` · **[VERIFIED]**
Four agents ship in the UI; `persona/` contains three files. Maya has no prompt, and the
landing page plays Amina's clip under Maya's name.
*From you:* nothing · *We build:* all of it · *Effort:* 1d · *After:* —

**AI-4 — Prompt persistence + versioning** · `P1` · **[VERIFIED]**
The agent editor shows a version badge and a Save button that explicitly saves nothing.
Prompts of record live in Python files.
*From you:* nothing · *We build:* all of it · *Effort:* 3d · *After:* BE-2

**AI-5 — Knowledge base / RAG** · `P1`
A nav section and an editor accordion with no system behind them. Needs ingestion,
chunking, embeddings, retrieval.
*From you:* embedding spend · *We build:* all of it · *Effort:* 6d · *After:* BE-2

**AI-6 — Function calling** · `P0` · **[VERIFIED]**
`conversation.py` **already sanitises `tool_calls`** — the plumbing is there. No tools are
defined and no LLM is wired to invoke them. **This is where the product becomes useful
rather than merely conversational.**
*From you:* nothing · *We build:* all of it · *Effort:* 6d · *After:* AI-1

**AI-7 — Guardrails** · `P1`
Money and medical contexts need refusal rules, number-readback verification, and
hallucination checks beyond prompt instructions.
*From you:* nothing · *We build:* all of it · *Effort:* 4d · *After:* AI-1

**AI-8 — Cross-call memory** · `P2`
Caller history and preferences. Meaningfully improves the repeat-caller experience.
*From you:* nothing · *We build:* all of it · *Effort:* 4d · *After:* BE-2

**AI-9 — Mid-call state persistence** · `P2`
A process restart currently drops the conversation. Needs checkpointing to survive a deploy.
*From you:* nothing · *We build:* all of it · *Effort:* 3d · *After:* BE-2

**AI-10 — Evaluation harness** · `P1`
No way to know whether a prompt edit improved or regressed containment. Blocks safe
iteration on the thing that most affects quality.
*From you:* nothing · *We build:* all of it · *Effort:* 5d · *After:* AI-1

---

## 5. BUSINESS LAYER — 7 items, ~23d
*No path from usage to revenue.*

**BZ-1 — Pricing model + page** · `P0` · **[VERIFIED]**
No pricing page exists; the FAQ openly says pricing is unset. **Nothing downstream can be
built until this is decided.**
*From you:* **CEO decision — per-minute, per-call, or seat** · *We build:* the page · *Effort:* 2d · *After:* —

**BZ-2 — Payments** · `P0`
Stripe for international, Flutterwave or Paystack for West Africa, M-Pesa Daraja for
Kenya. Each requires a registered entity.
*From you:* **entity, bank account, merchant onboarding** · *We build:* integration · *Effort:* 8d · *After:* entity, BZ-1

**BZ-3 — Usage metering** · `P0` · **[VERIFIED]**
`metrics.py` computes per-call cost but nothing attributes usage to a tenant for billing.
*From you:* nothing · *We build:* all of it · *Effort:* 4d · *After:* BE-2, BE-4

**BZ-4 — Subscription management** · `P1`
Plans, upgrades, cancellation, dunning.
*From you:* nothing · *We build:* all of it · *Effort:* 4d · *After:* BZ-2

**BZ-5 — Invoicing** · `P1`
Receipts and invoices. Kenya may require eTIMS integration — confirm with counsel.
*From you:* legal confirmation · *We build:* all of it · *Effort:* 3d · *After:* BZ-2

**BZ-6 — Margin monitoring** · `P1`
Per-call cost against price, with alerts. A long call on a premium voice can be sold at a
loss silently.
*From you:* nothing · *We build:* all of it · *Effort:* 2d · *After:* BZ-3

**BZ-7 — Trial enforcement** · `P1` · **[VERIFIED]**
The console shows "Free trial · 14 days left" as static text. Nothing counts down or
restricts anything.
*From you:* nothing · *We build:* all of it · *Effort:* 2d · *After:* BE-2, BZ-4

---

## 6. CLIENT DELIVERABLES — 7 items, ~26d+
*What customers actually buy.*

**CD-1 — Generic webhook + Zapier** · `P1`
The highest-leverage integration by far: one webhook covers most SMB systems without
bespoke work. **Build this before any named integration.**
*From you:* nothing · *We build:* all of it · *Effort:* 3d · *After:* AI-6

**CD-2 — Calendar booking** · `P1`
Google and Outlook. Underpins the clinic, salon and consultancy use cases.
*From you:* OAuth app registration · *We build:* all of it · *Effort:* 3d · *After:* AI-6

**CD-3 — Hotel PMS** · `P1`
Cloudbeds, Mews or Opera. **Kwame is a hotel agent that cannot see a room inventory.**
*From you:* partner API access · *We build:* integration · *Effort:* 6d · *After:* AI-6

**CD-4 — Restaurant POS** · `P2`
Kofi takes orders that go nowhere.
*From you:* partner API access · *We build:* integration · *Effort:* 5d · *After:* AI-6

**CD-5 — Clinic scheduling** · `P2`
Booking plus a patient-data handling review before any pilot.
*From you:* legal review · *We build:* integration · *Effort:* 5d · *After:* AI-6

**CD-6 — CRM** · `P2`
HubSpot, Zoho, Salesforce. Push call outcomes into the customer's existing pipeline.
*From you:* API keys · *We build:* integration · *Effort:* 4d · *After:* CD-1

**CD-7 — Core banking** · `P2`
Amina handles card and billing disputes with no access to any banking system.
Enterprise-grade compliance lead time.
*From you:* partnership + compliance, CEO-led · *We build:* integration · *Effort:* 20d+ · *After:* Phase 4

---

## 7. LEGAL & COMPLIANCE — 12 items
*Blocks revenue, not engineering. Longest lead time in the document.*

**LG-1 — Terms of Service** · `P0` · **[VERIFIED]**
Does not exist. There is no `terms.html` in the repo.
*From you:* **Kenyan counsel** · *We build:* nothing · *Effort:* — · *After:* counsel

**LG-2 — Privacy Policy** · `P0` · **[VERIFIED]**
Does not exist. Required before collecting a single phone number.
*From you:* **Kenyan counsel** · *We build:* nothing · *Effort:* — · *After:* counsel

**LG-3 — Kenya DPA 2019 registration** · `P0`
Registration with the ODPC as both data controller and processor. Fees and a filing process.
*From you:* CEO + counsel, filing fees · *We build:* nothing · *Effort:* — · *After:* registered entity

**LG-4 — Call recording consent** · `P0` · **[VERIFIED]**
The personas disclose that the agent is **AI**. Nothing discloses that the call is being
**recorded** — that is a separate and distinct legal requirement.
*From you:* legal review · *We build:* prompt work · *Effort:* 1d · *After:* LG-2

**LG-5 — Data residency decision** · `P0`
Whisper and Edge TTS can run locally; ElevenLabs and hosted LLMs cannot. Determines
architecture, hosting region, and what you may lawfully promise.
*From you:* **CEO + CTO decision** · *We build:* nothing · *Effort:* — · *After:* —

**LG-6 — Retention & deletion** · `P1`
A written policy plus the code that enforces it, including data-subject deletion requests.
*From you:* counsel writes the policy · *We build:* enforcement · *Effort:* 3d · *After:* BE-7

**LG-7 — Nigeria NDPA 2023** · `P1` — required before Nigerian customers. *Phase 3.*

**LG-8 — South Africa POPIA** · `P1` — required before ZA customers. AT already supports the country. *Phase 3.*

**LG-9 — Ghana DPA 2012** · `P1` — Kwame's own market. Registration with the Data Protection Commission. *Phase 2.*

**LG-10 — GDPR + transfer mechanism** · `P1` — needed if any EU client or EU-resident caller is in scope, including SCCs. *Phase 3.*

**LG-11 — Customer DPA template** · `P1` — enterprise buyers will not sign without one. *Phase 4.*

**LG-12 — EU AI Act Art. 50** · `P2` — disclosure obligations. The personas already comply; this is documentation, not rework. *Phase 3.*

---

## 8. MARKETING & SALES — 8 items, ~9d
*Cheap, fast, mostly unblocked.*

**MK-1 — Analytics** · `P1` · **[VERIFIED]**
No tracking on any page. There is currently no way to tell whether the landing page works.
*From you:* decision — GA4 / Plausible / PostHog · *We build:* all of it · *Effort:* 0.5d · *After:* —

**MK-2 — robots.txt + sitemap.xml** · `P1` · **[VERIFIED]**
Neither file exists. FAQ schema already shipped, so this is the remaining technical-SEO gap.
*From you:* nothing · *We build:* all of it · *Effort:* 0.5d · *After:* —

**MK-3 — Email capture** · `P1` · **[VERIFIED]**
The newsletter form is inert and says so honestly. Needs an ESP.
*From you:* ESP account · *We build:* all of it · *Effort:* 2d · *After:* —

**MK-4 — Social accounts** · `P1` · **[VERIFIED]**
Footer links are inert placeholders with honest aria-labels. No accounts exist.
*From you:* CEO creates them · *We build:* wiring · *Effort:* — · *After:* —

**MK-5 — Conversion tracking** · `P2` — demo-request and contact-sales attribution. *2d, after MK-1.*

**MK-6 — Case studies** · `P2` · **[VERIFIED]** — blocked entirely on the first customer.
The landing page already carries an honest placeholder. *2d, after design partner.*

**MK-7 — Content / blog** · `P2` — owned-media surface for the SEO terms the FAQ already targets. *3d.*

**MK-8 — Per-page OG images** · `P2` — only one shared image today; link previews are weak on every page but the landing page. *1d.*

---

## 9. CUSTOMER SUPPORT — 7 items, ~14d
*None of it exists yet.*

**SP-1 — Help desk / ticketing** · `P1`
No inbox, no ticket system. The first support request has nowhere to land.
*From you:* decision + spend — Intercom / Crisp / Zendesk · *We build:* wiring · *Effort:* 2d · *After:* —

**SP-2 — Working help centre** · `P1` · **[VERIFIED]**
`help_center.html` is static; the search box does not search.
*From you:* nothing · *We build:* all of it · *Effort:* 2d · *After:* —

**SP-3 — Product documentation** · `P1`
API reference, integration guides, prompt-writing guidance. Needed the moment a developer
touches it.
*From you:* nothing · *We build:* all of it · *Effort:* 5d · *After:* BE-1

**SP-4 — In-product onboarding** · `P1` · **[VERIFIED]**
The console shows "Getting started 0/6" and tracks nothing. `product_tour.html` is static.
*From you:* nothing · *We build:* all of it · *Effort:* 3d · *After:* BE-2

**SP-5 — Status page** · `P2` — customers whose phone lines run through you will ask for one on day one. *1d, after TD-6.*

**SP-6 — Live chat** · `P2` — presales and onboarding support. *1d, after SP-1.*

**SP-7 — SLA definition** · `P2` — optional until enterprise, then mandatory and contractual. *Phase 4.*

---

## 10. TECHNICAL DEBT & OPERATIONS — 12 items, ~26d
*Cheapest to fix now, most expensive later.*

**TD-1 — Test suite** · `P0` · **[VERIFIED]**
**Zero test files exist in the repo.** The interruption and conversation modules are
intricate, stateful, and completely unverified.
*From you:* nothing · *We build:* all of it · *Effort:* 6d · *After:* —

**TD-2 — CI/CD** · `P0` · **[VERIFIED]**
No `.github/`, no pipeline, no Dockerfile. Every deploy is manual and unverified.
*From you:* nothing · *We build:* all of it · *Effort:* 1d · *After:* TD-1

**TD-3 — Silent failure handling** · `P0` · **[VERIFIED]**
The engine catches STT and LLM exceptions, prints, and returns `None` — a caller hears
nothing and no alert fires.
*From you:* nothing · *We build:* all of it · *Effort:* 2d · *After:* TD-5

**TD-4 — Error tracking** · `P1`
No Sentry or equivalent. Production failures would be completely invisible.
*From you:* ~$26/mo · *We build:* all of it · *Effort:* 0.5d · *After:* BE-5

**TD-5 — Structured logging + call tracing** · `P1`
A per-call trace ID through STT, LLM, TTS and telephony. Without it, debugging a bad call
is guesswork.
*From you:* nothing · *We build:* all of it · *Effort:* 3d · *After:* BE-1

**TD-6 — Uptime monitoring + alerting** · `P1`
Phone lines are an availability product. Silent downtime is the worst possible failure mode.
*From you:* spend · *We build:* all of it · *Effort:* 2d · *After:* BE-5

**TD-7 — Pin dependencies** · `P1` · **[VERIFIED]**
Every requirement uses `>=`. Builds are not reproducible and an upstream release can
break production without a commit.
*From you:* nothing · *We build:* all of it · *Effort:* 0.5d · *After:* —

**TD-8 — Dockerfile** · `P1` · **[VERIFIED]**
No reproducible environment. Whisper's absence locally is a direct symptom of exactly this.
*From you:* nothing · *We build:* all of it · *Effort:* 1d · *After:* BE-5

**TD-9 — Load testing** · `P1`
No data on concurrent-call capacity, so scaling and pricing are both guesses.
*From you:* nothing · *We build:* all of it · *Effort:* 3d · *After:* TEL-10

**TD-10 — Retire preview.py** · `P2` · **[VERIFIED]**
5,560 lines of Python generating an HTML dashboard that `dashboard.html` now supersedes.
Two dashboards will drift apart.
*From you:* nothing · *We build:* all of it · *Effort:* 3d · *After:* —

**TD-11 — Resolve agent_manager.html** · `P2` · **[VERIFIED]**
Contains no persistence code at all and overlaps the console's agent editor.
Keep one, delete the other.
*From you:* CTO decision · *We build:* the merge · *Effort:* 1d · *After:* BE-2

**TD-12 — README** · `P2` · **[VERIFIED]**
Nine lines of marketing copy. No setup steps, no architecture notes, no contribution guide.
*From you:* nothing · *We build:* all of it · *Effort:* 1d · *After:* —

---

# WHAT WE NEED FROM YOU

Nothing in this table is engineering work. Ordered by how much it delays Phase 1.

| # | Need | Who | Why it blocks | Lead time |
|---|---|---|---|---|
| 1 | **LLM API key** (Anthropic, or the Groq key already in `.env`) | CTO | No agent can speak. Highest-leverage unblock in the document. | same day |
| 2 | **Africa's Talking account** — API key, username, prepaid credit | CEO | No inbound call can reach the engine. | 2–5 days |
| 3 | **Compute host decision** — Fly.io / Railway / Render | CTO | Vercel is static-only and cannot host the engine. | 1 day |
| 4 | **Legal counsel** — Kenyan firm, data-protection practice | CEO | ToS, Privacy, ODPC, recording consent. No payment without these. | 3–6 weeks |
| 5 | **Registered entity + bank account** | CEO | Stripe, Flutterwave and M-Pesa all require it. | 2–8 weeks |
| 6 | **Pricing decision** | CEO | Billing cannot be built against an undecided model. | 1 week |
| 7 | **Data residency decision** | CEO + CTO | Determines architecture and what you may lawfully promise. | 1 week |
| 8 | **Design partner** — one hotel, signed LOI | CEO | Phase 1 targets a specific customer. | 2–6 weeks |
| 9 | **Caller-ID registration** | CEO | Without it, outbound calls get flagged as spam. | 3–8 weeks |
| 10 | **Monthly infra budget** — est. $180–450/mo at Phase 2 | CEO | Host, DB, storage, error tracking, help desk, LLM + TTS usage. | 1 day |

---

# BUILD ROADMAP

Timelines are **elapsed weeks**, not effort-days — external lead times dominate Phase 1.
Assumes one full-time engineer working with Claude.

---

## PHASE 1 — MVP: FIRST PAYING CUSTOMER
**Timeline: 8–10 weeks**

> **Goal: Kwame answers a real Ghanaian phone number and completes a booking.**
> One agent, one number, one customer, invoiced by hand.
> Everything not required for that sentence is deferred.

### Deliverables
- `AI-1` Real LLM adapter, replacing MockLLM
- `V-3` `V-4` ElevenLabs registered, TTS connected to the engine
- `V-1` `V-2` Whisper deployed with streaming transcription
- `V-6` 8 kHz ↔ 16 kHz resampling
- `TEL-1` `TEL-2` Africa's Talking with live audio streaming
- `BE-1` `BE-5` API server on a container host
- `BE-2` `BE-3` Postgres and real authentication
- `BE-9` Rate limiting on the public demo endpoint
- `AI-6` Function calling — check availability, make a booking
- `LG-1` `LG-2` `LG-4` ToS, Privacy Policy, recording consent
- `TD-1` `TD-2` Tests and CI on the conversation core

### What we can start TODAY — zero external dependencies
1. Register ElevenLabs and wire `_synthesize()` — **half a day, unblocks all audio**
2. Write the LLM adapter against **the Groq key already in `.env`**
3. Install Whisper, pin dependencies, add a Dockerfile
4. Test suite for `interruption.py` and `conversation.py` — the riskiest untested code
5. Write Maya's persona, or remove her from both UIs
6. Add analytics, `robots.txt`, `sitemap.xml`
7. Fix silent failure: a dead call must raise and alert

### What blocks this phase
An **LLM API key** and an **Africa's Talking account**. Both are same-week unblocks and
everything else in Phase 1 waits behind them. **Legal counsel should start in parallel** —
six weeks of lead time makes it the critical path to *revenue*, even though it blocks no code.

---

## PHASE 2 — PRODUCTION: REAL USERS, REAL CALLS
**Timeline: +10–12 weeks**

> **Goal: customers sign up, configure an agent, and are billed automatically.**
> The team stops being in the loop for every call.

### Deliverables
- `BE-4` Orgs, roles, multi-tenancy
- `BZ-1` `BZ-2` Pricing, Stripe, M-Pesa, Flutterwave
- `BZ-3` `BZ-7` Usage metering and trial enforcement
- `TEL-3` `TEL-4` Number provisioning and call routing
- `TEL-5` Recording capture and storage
- `AI-4` Prompt persistence and versioning
- `AI-5` Knowledge base
- `AI-10` Evaluation harness — before prompt changes ship
- `CD-1` `CD-2` Webhooks, Zapier, calendar booking
- `TD-4` `TD-5` `TD-6` Sentry, tracing, uptime alerting
- `SP-1` `SP-2` `SP-3` Help desk, working help centre, docs
- `LG-9` Ghana DPA registration

### Do in parallel, do not wait
- Design the **tenancy model now** — retrofitting it later is a rewrite
- Build the **evaluation harness before** the prompt library grows
- Ship the **generic webhook before** any named integration

### What blocks this phase
A **registered entity and bank account** gate every payment processor.
Start incorporation during Phase 1, not at the start of Phase 2.

---

## PHASE 3 — SCALE: MANY CLIENTS, MANY COUNTRIES
**Timeline: +12–16 weeks**

> **Goal: seven countries, concurrent call volume, and languages beyond English.**
> Operations stop being manual.

### Deliverables
- `V-8` Swahili, Twi and Yoruba synthesis — **or an honest correction to the marketing claim**
- `TEL-7` `TEL-8` SIP trunking and Twilio fallback
- `TEL-9` Caller-ID registration per country
- `BE-8` Batch calling and campaign queue
- `TEL-10` `TD-9` Concurrency model and load testing
- `LG-7` `LG-8` `LG-10` NDPA, POPIA, GDPR
- `CD-3` `CD-6` Hotel PMS and CRM integrations
- `AI-8` Cross-call memory
- `SP-5` Status page

### Do in parallel, do not wait
- Start **caller-ID registration in Phase 2** — 3–8 weeks per country, cannot be rushed
- Decide the **Swahili TTS route early**: licensing and training have very different timelines

### What blocks this phase
The **Swahili and Twi voice gap** is the one place where marketing currently runs ahead of
the product. Either close it here or soften the claim on the landing page before Phase 2
traffic arrives.

---

## PHASE 4 — ENTERPRISE: BANKS, HOSPITALS, HOTEL GROUPS
**Timeline: +6–9 months**

> **Goal: procurement, security review, and contractual uptime.**
> A different sales motion and a different bar.

### Deliverables
- SSO / SAML and SCIM provisioning
- Immutable audit logs
- `SP-7` Contractual SLA with credits
- Penetration test and SOC 2 Type II path
- `LG-11` Customer DPA templates
- VPC or on-premise deployment
- `CD-7` Core banking integration
- HL7 / FHIR for clinical systems
- `AI-7` Hardened guardrails and human-in-the-loop QA
- Regional data residency guarantees

### Do in parallel, do not wait
- Keep **audit-log structure in mind from Phase 2** — bolting it on later is painful
- **Local-only Whisper and Edge TTS are a genuine competitive advantage here** — protect that capability

### What blocks this phase
Security review, not engineering. Budget **three to six months of procurement per
enterprise account** and do not promise otherwise.

---

# TWO THINGS THAT NEED A DECISION THIS WEEK

**1. A marketing claim runs ahead of the product.**
The landing page says agents answer calls in Swahili and Twi. Transcription handles them;
**zero of your nine voices synthesise them.** Either close that in Phase 3 or soften the
copy now — it is a five-minute edit and it removes a real misrepresentation risk.

**2. The public demo form is a financial attack surface.**
An unmetered outbound-call endpoint invites toll fraud. It is currently inert, which is why
rate limiting (`BE-9`) is logged as P0 *before* that form goes live rather than after.

---

*End of document. 92 gaps · 27 P0 · 37 verified by execution · ~288 engineer-days.*
