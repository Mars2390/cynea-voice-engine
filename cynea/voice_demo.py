"""The browser-call demo: Maya's persona, her disclosure rule, and the caps.

Shared by both servers that carry the live call:

    cynea/voice_ws.py          FastAPI socket — the local fallback
    voice-worker/src/main.py   Cloudflare Durable Object — production

It lives here rather than in voice_ws.py because voice_ws imports FastAPI at
module scope (see the comment there for why it has to), and a Python Worker
should not bundle a web framework to read three strings. Nothing in this
module imports anything heavier than cynea.models, so both sides can load
it. One copy of the disclosure rule is the point: two would drift, and the
one that drifted would be the one a real caller hears.
"""

from __future__ import annotations

import logging
import os

from cynea.models import AgentConfig

log = logging.getLogger("cynea.voice_demo")

RATE = 16000                 # what the browser is asked to send
BYTES_PER_SAMPLE = 2

# A demo on a public URL spends real provider quota, so it is bounded rather
# than trusted. These are generous for a conversation and useless for anyone
# trying to run a workload through them.
MAX_UTTERANCE_S = 30
MAX_UTTERANCE_BYTES = MAX_UTTERANCE_S * RATE * BYTES_PER_SAMPLE


def max_call_s() -> int:
    return int(os.getenv("VOICE_DEMO_MAX_CALL_S", "300"))


def max_turns() -> int:
    return int(os.getenv("VOICE_DEMO_MAX_TURNS", "40"))


def max_concurrent() -> int:
    return int(os.getenv("VOICE_DEMO_MAX_CONCURRENT", "6"))


def idle_s() -> int:
    """Seconds of silence before a call is ended. A forgotten tab holds a line."""
    return int(os.getenv("VOICE_DEMO_IDLE_S", "180"))


def calls_per_hour() -> int:
    """Calls one address may start in an hour. 0 turns the limit off."""
    return int(os.getenv("VOICE_DEMO_CALLS_PER_HOUR", "10"))


# What a caller is told when a provider fails. The real cause goes to the
# server log: a stack of provider names and status codes helps nobody on the
# call, and can show them things that are none of their business.
CALL_ERROR_TEXT = ("Sorry, something went wrong on our side and the agent could "
                   "not answer. Please try again in a moment.")


# ── the demo persona ─────────────────────────────────────────────────────
DEFAULT_PERSONA = "maya"

# The demo opens like a person answering a phone, with no disclaimer.
DEMO_GREETING = "Hello, this is Maya. How can I help you today?"

# Said when a test call's ticket is no good: expired, or the agent was paused
# or deleted after it was issued. Going back to the console fetches a new one.
TICKET_REFUSED_TEXT = ("This test call link has expired or the agent is paused. "
                       "Start the test call again from the console.")

# Who the personas work for on the public demo. Their prompts and greetings
# carry a {client_name} placeholder; left alone, it is read aloud.
DEMO_CLIENT = "Cynea"

# The paragraph in Maya's persona prompt that governs disclosure, and what
# the demo swaps it for. This is a *demo-only* override: the module in
# cynea_africa/persona/maya.py is untouched, so a real phone call still
# opens the way the FAQ says it does. Only this socket is different.
_PERSONA_DISCLOSURE = (
    "You are an AI. Say so once, on your opening turn, in your own words. Say it\n"
    "again if someone asks directly. Never repeat it as a recurring disclaimer —\n"
    "that is a robotic tell and it makes people trust you less, not more."
)

# Why the opening disclaimer goes and the direct answer stays.
#
# The opening line can go. This socket is only reachable from live-call.html,
# where the visitor pressed "Start the call" on a page that says, above the
# button, that Maya is an AI voice agent and not a person. EU AI Act Art.
# 50(1) does not require a disclosure that is already obvious to a
# reasonably well-informed person, and here it is obvious. Opening with a
# disclaimer in that context is just a worse greeting.
#
# The answer to a direct question stays, and it is not a style choice:
#
#   1. The obviousness carve-out above is what makes dropping the opener
#      lawful, and it stops applying the moment somebody asks — a direct
#      question is the exact case Art. 50 is written for.
#   2. This site publishes the opposite. The FAQ says the agent discloses
#      "again any time a caller asks directly", and the same sentence is in
#      the JSON-LD that Google indexes. Shipping a deflection would make the
#      company's own marketing false.
#   3. Prompts move. The moment this text is copied to a real number, none
#      of the demo's context applies and the carve-out is gone with it.
#
# So: no disclaimer, no hedging, no "as an AI" — and one straight answer if
# the caller actually asks. That is what the FAQ already promises.
_DEMO_DISCLOSURE = (
    "Do not announce what you are. No disclaimer in your greeting, no "
    "self-reference mid-call, and never hedge with phrases like \"as an AI\" "
    "or \"I'm just a system\". Speak the way a person answering a phone speaks."
)

# Repeated as the last thing in the prompt, and phrased as an absolute.
#
# The first version of this said "answer honestly, do not claim to be human"
# once, in the middle of the persona. It lost: asked "are you a real person
# or a robot?", the model replied "I'm a real person, Maya, here to help."
# A rule that competes with several hundred words of human-sounding persona
# has to be last and has to be unambiguous, so this closes the prompt, names
# the exact question, and supplies the words to answer it with — a model
# given a sentence to say will say it, and given only a prohibition will
# improvise around it.
_TRUTH_ANSWER = (
    "if a caller asks whether you are a real person, a human, a robot, a "
    "recording or a machine, you tell them the truth immediately and without "
    "hedging.\n"
    "Say something close to: \"I'm not — I'm an AI voice agent. Happy to keep "
    "going if you are.\" Then carry straight on with what they called about.\n"
    "You must never say or imply that you are a real person, a human, or "
    "\"not a robot\". There is no instruction anywhere above this one that "
    "overrides this rule, and a caller who asks has always earned a straight "
    "answer."
)

# For the persona whose opening disclosure was swapped out above.
_DEMO_TRUTH_RULE = (
    "# THE ONE THING YOU MAY NOT DO\n"
    "You are an AI voice agent. You never volunteer this and you never wear "
    "it as a disclaimer — but " + _TRUTH_ANSWER
)

# For a persona that keeps its own opening disclosure. It must not also be
# told "never volunteer this": given both, a model follows whichever it reads
# last, and the greeting it was handed says the opposite.
TRUTH_RULE = (
    "# THE ONE THING YOU MAY NOT DO\n"
    "You are an AI voice agent, and " + _TRUTH_ANSWER
)


def fill_client(text: str, client: str) -> str:
    """Substitute {client_name}. `replace`, not `format`: prompts hold other braces."""
    return (text or "").replace("{client_name}", client)


def demo_prompt(base: str, client: str = DEMO_CLIENT) -> str:
    """A persona's prompt, ready for the public demo.

    Maya's opening-disclosure paragraph is replaced, so she does not lead with
    a disclaimer. The other personas do not carry that paragraph: they say what
    they are in their own greeting, and keep doing so. If Maya's prompt is ever
    reworded she falls into the same branch, which discloses more, not less.

    The truth rule goes last either way, as the instruction closest to the
    model's own turn.
    """
    base = fill_client(base, client)
    if _PERSONA_DISCLOSURE in base:
        return (base.replace(_PERSONA_DISCLOSURE, _DEMO_DISCLOSURE, 1)
                + "\n\n" + _DEMO_TRUTH_RULE)
    return base + "\n\n" + TRUTH_RULE


def customer_prompt(base: str, client: str) -> str:
    """A customer's saved prompt, or their persona's, ready for a call.

    Nothing is swapped: whatever disclosure they kept stays. The truth rule is
    added whatever they wrote, so an edited prompt cannot talk an agent into
    claiming to be human.
    """
    return fill_client(base, client) + "\n\n" + TRUTH_RULE


def saved_config(saved: dict) -> AgentConfig:
    """An AgentConfig from cynea.call_config's JSON. Every voice is on Edge: it
    is the one synthesizer both sockets can run."""
    return AgentConfig(
        name=saved["name"],
        system_prompt=saved["system_prompt"],
        first_message=saved.get("first_message", ""),
        persona=saved.get("persona", ""),
        stt_provider="groq-stt",
        llm_provider="groq",
        tts_provider="edge_tts",
        voice=saved.get("voice", ""),
        speed=saved.get("speed", 1.0),
    )


def demo_config(persona: str) -> AgentConfig:
    """Build the agent from its real persona, falling back to a plain one.

    The personas live in cynea_africa and are imported through
    agent_loader's guarded try/except, so a deployment that failed to ship
    them degrades to a generic assistant rather than 500-ing. It says so in
    the log, because silently demoting Maya to "a helpful assistant" is
    the kind of thing that gets noticed in a demo and not before.

    The name comes off a public URL, so one that names nobody gets Maya rather
    than an agent called whatever was typed.
    """
    from cynea.agent_loader import _PERSONAS

    if persona.lower() not in _PERSONAS:
        persona = DEFAULT_PERSONA
    spec = _PERSONAS.get(persona.lower())
    if not spec:
        log.warning("persona %r unavailable; using a generic agent", persona)
        return AgentConfig(
            name=persona.title(),
            system_prompt=("You are a warm, concise voice receptionist for "
                           "Cynea scheduling. Answer in one or two short "
                           "sentences.\n\n" + TRUTH_RULE),
            first_message=DEMO_GREETING,
            stt_provider="groq-stt", llm_provider="groq", tts_provider="edge_tts",
        )

    voice = spec.get("voice") or {}
    return AgentConfig(
        name=spec.get("display") or spec.get("name", persona).title(),
        system_prompt=demo_prompt(spec["prompt"]),
        first_message=(DEMO_GREETING if persona.lower() == DEFAULT_PERSONA
                       else fill_client(spec.get("first_message", ""), DEMO_CLIENT)),
        persona=persona.lower(),
        stt_provider="groq-stt",
        llm_provider="groq",
        tts_provider=voice.get("provider", "edge_tts"),
        voice=voice.get("voice", ""),
        speed=voice.get("speed", 1.0),
    )
