"""Cynea Voice Engine — agent loader.

Reads a JSON config (file or dict), pairs it with a persona module, and
returns a fully-wired CyneaEngine ready to take calls. Equivalent role
to Bolna's agent JSON system, but Cynea-shaped.

Persona registry
----------------
Each persona contributes three pieces of data:
    prompt          — the system prompt template (may contain a
                      {client_name} placeholder; the loader substitutes)
    voice           — default voice config dict (provider, voice_id, speed)
    first_message   — fallback opening line when the JSON omits one

Adding a new persona is one line at the bottom of this file:
    _register("amara", AMARA_SYSTEM_PROMPT, AMARA_VOICE_CONFIG, AMARA_FIRST_MESSAGE)

Usage
-----
    from cynea.agent_loader import AgentLoader

    loader = AgentLoader()
    engine = loader.load_from_file("examples/agent_config.json")
    first  = await engine.start()

The loader validates the config before instantiating CyneaEngine, so a
malformed JSON fails at load time rather than mid-call.
"""

from __future__ import annotations

import copy
import json
import os
import re
from typing import Dict, List, Optional

from cynea.engine import CyneaEngine
from cynea.models import AgentConfig


# ---------------------------------------------------------------------
# Persona registry
# ---------------------------------------------------------------------

_PERSONAS: Dict[str, dict] = {}


def _register(name: str, prompt: str, voice_config: dict, first_message: str) -> None:
    """Register a persona. Called at module import time."""
    _PERSONAS[name.lower()] = {
        "name": name.lower(),
        "prompt": prompt,
        "voice": dict(voice_config),
        "first_message": first_message,
    }


# Import each persona module guarded so a missing file doesn't crash the
# whole loader — the failing persona is just unavailable.
try:
    from cynea_africa.persona.kwame import (
        KWAME_SYSTEM_PROMPT,
        KWAME_VOICE_CONFIG,
        KWAME_FIRST_MESSAGE,
    )
    _register("kwame", KWAME_SYSTEM_PROMPT, KWAME_VOICE_CONFIG, KWAME_FIRST_MESSAGE)
except ImportError:
    pass

try:
    from cynea_africa.persona.amina import (
        AMINA_SYSTEM_PROMPT,
        AMINA_VOICE_CONFIG,
        AMINA_FIRST_MESSAGE,
    )
    _register("amina", AMINA_SYSTEM_PROMPT, AMINA_VOICE_CONFIG, AMINA_FIRST_MESSAGE)
except ImportError:
    pass

try:
    from cynea_africa.persona.kofi import (
        KOFI_SYSTEM_PROMPT,
        KOFI_VOICE_CONFIG,
        KOFI_FIRST_MESSAGE,
    )
    _register("kofi", KOFI_SYSTEM_PROMPT, KOFI_VOICE_CONFIG, KOFI_FIRST_MESSAGE)
except ImportError:
    pass

try:
    from cynea_africa.persona.maya import (
        MAYA_SYSTEM_PROMPT,
        MAYA_VOICE_CONFIG,
        MAYA_FIRST_MESSAGE,
    )
    _register("maya", MAYA_SYSTEM_PROMPT, MAYA_VOICE_CONFIG, MAYA_FIRST_MESSAGE)
except ImportError:
    pass


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------

_REQUIRED_TOP_LEVEL = ("agent_name", "persona")
# E.164: '+' then a country-code digit, then 6-14 more digits OR 'X'
# placeholders. The 'X' relaxation lets template configs validate even
# when the operator hasn't filled in their real number yet.
_E164_RE = re.compile(r"^\+\d[\dX]{6,14}$")


def _validate_config(config: dict) -> None:
    """Raise ValueError with a clear message if the config is malformed.

    Validation is intentionally conservative — anything that would cause
    the engine to crash at runtime gets flagged here. Anything purely
    advisory (escalation numbers, location strings) is checked loosely
    so we don't reject configs over cosmetic typos.
    """
    if not isinstance(config, dict):
        raise ValueError(
            f"Agent config must be a JSON object, got {type(config).__name__}."
        )

    missing = [k for k in _REQUIRED_TOP_LEVEL if not config.get(k)]
    if missing:
        raise ValueError(f"Agent config missing required field(s): {missing}")

    persona = (config.get("persona") or "").lower()
    if persona not in _PERSONAS:
        raise ValueError(
            f"Unknown persona '{persona}'. "
            f"Available: {sorted(_PERSONAS.keys())}"
        )

    voice = config.get("voice") or {}
    if not isinstance(voice, dict):
        raise ValueError("`voice` must be an object with provider/voice_id/speed.")

    speed = voice.get("speed", 1.0)
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        raise ValueError(f"`voice.speed` must be numeric, got {speed!r}.")
    if not (0.5 <= speed <= 2.0):
        raise ValueError(
            f"`voice.speed` must be between 0.5 and 2.0, got {speed}."
        )

    max_dur = config.get("max_call_duration", 600)
    try:
        max_dur = int(max_dur)
    except (TypeError, ValueError):
        raise ValueError(
            f"`max_call_duration` must be an integer (seconds), got {max_dur!r}."
        )
    if max_dur <= 0:
        raise ValueError("`max_call_duration` must be > 0.")

    escalation = config.get("escalation_number")
    if escalation and not _E164_RE.match(escalation):
        raise ValueError(
            f"`escalation_number` must be E.164 (e.g. '+254XXXXXXXXX'), got "
            f"{escalation!r}."
        )


# ---------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------

class AgentLoader:
    """Builds a CyneaEngine from a JSON config + a persona module."""

    # ----- discovery --------------------------------------------------

    @staticmethod
    def list_available_personas() -> List[str]:
        """Return the personas registered at import time."""
        return sorted(_PERSONAS.keys())

    @staticmethod
    def get_persona_config(name: str) -> dict:
        """Return a deep copy of the persona's prompt/voice/first_message.

        Raises:
            KeyError: if no persona with that name is registered.
        """
        key = (name or "").lower()
        if key not in _PERSONAS:
            raise KeyError(
                f"No persona named '{name}'. Available: {sorted(_PERSONAS.keys())}"
            )
        return copy.deepcopy(_PERSONAS[key])

    # ----- loading ----------------------------------------------------

    def load_from_file(self, path: str) -> CyneaEngine:
        """Read a JSON file and return a configured CyneaEngine."""
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Agent config not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            try:
                config = json.load(f)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Agent config at {path} is not valid JSON: {exc}"
                ) from exc
        return self.load_from_dict(config)

    def load_from_dict(self, config: dict) -> CyneaEngine:
        """Build a CyneaEngine from an in-memory config dict."""
        _validate_config(config)

        persona = self.get_persona_config(config["persona"])
        voice = dict(config.get("voice") or {})
        client_name = config.get("client_name") or "the client"

        # Persona prompt with {client_name} substituted (safe even when
        # the prompt has braces that aren't placeholders, because we use
        # `replace`, not `str.format`).
        system_prompt = persona["prompt"].replace("{client_name}", client_name)

        agent_config = AgentConfig(
            name=config["agent_name"],
            system_prompt=system_prompt,
            stt_provider=config.get("stt_provider", "whisper"),
            # Was "mock", which meant every loaded agent silently used the
            # test double even once a real adapter existed. Configs that
            # genuinely want the double still ask for it by name.
            llm_provider=config.get("llm_provider", "groq"),
            tts_provider=voice.get("provider") or persona["voice"].get("provider", "edge_tts"),
            voice=voice.get("voice_id") or persona["voice"].get("voice", "en-GB-RyanNeural"),
            speed=float(voice.get("speed", persona["voice"].get("speed", 1.0))),
            first_message=config.get("first_message") or persona["first_message"],
            interruption_enabled=bool(config.get("interruption_enabled", True)),
            backchanneling_enabled=bool(config.get("backchanneling_enabled", True)),
            persona=persona["name"],
            client_name=config.get("client_name"),
            location=config.get("location"),
            max_call_duration=int(config.get("max_call_duration", 600)),
            escalation_number=config.get("escalation_number"),
        )
        return CyneaEngine(agent_config)


# Convenience module-level helpers for quick scripts.
def load_agent(path_or_dict) -> CyneaEngine:
    """One-liner: pass a path or a dict, get a CyneaEngine."""
    loader = AgentLoader()
    if isinstance(path_or_dict, dict):
        return loader.load_from_dict(path_or_dict)
    return loader.load_from_file(path_or_dict)


__all__ = ["AgentLoader", "load_agent"]
