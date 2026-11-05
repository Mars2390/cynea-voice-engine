"""What the public demo tells callers: the prompt, the greeting and the voice.

Each of these was wrong on the live demo at some point: a {client_name} read
aloud, two personas told both "say you are an AI" and "never say so", and a
persona on a voice provider the Cloudflare socket cannot run.
"""

import pytest

from cynea.agent_loader import _PERSONAS
from cynea.voice_demo import (
    CALL_ERROR_TEXT,
    DEFAULT_PERSONA,
    DEMO_CLIENT,
    TRUTH_RULE,
    calls_per_hour,
    demo_config,
    demo_prompt,
    fill_client,
    idle_s,
)

PERSONAS = ["maya", "kwame", "amina", "kofi"]


@pytest.mark.parametrize("persona", PERSONAS)
def test_no_placeholder_reaches_the_model_or_the_caller(persona):
    config = demo_config(persona)
    assert "{client_name}" not in config.system_prompt
    assert "{client_name}" not in config.first_message


@pytest.mark.parametrize("persona", PERSONAS)
def test_truth_rule_is_the_last_thing_in_every_prompt(persona):
    prompt = demo_config(persona).system_prompt
    assert prompt.rstrip().endswith("has always earned a straight answer.")
    assert "I'm an AI voice agent" in prompt.rsplit("# THE ONE THING YOU MAY NOT DO", 1)[1]


def test_maya_alone_stays_quiet_about_it_until_asked():
    """She is the one persona whose greeting does not say what she is."""
    config = demo_config("maya")
    assert "You never volunteer this" in config.system_prompt
    assert "Say so once, on your opening turn" not in config.system_prompt


@pytest.mark.parametrize("persona", ["kwame", "amina", "kofi"])
def test_the_others_are_not_told_to_hide_what_their_greeting_says(persona):
    config = demo_config(persona)
    assert " AI" in config.first_message
    assert "You never volunteer this" not in config.system_prompt
    assert "Do not announce what you are" not in config.system_prompt


def test_a_reworded_persona_discloses_more_not_less():
    assert demo_prompt("You are someone new.").endswith(TRUTH_RULE)


def test_client_name_is_filled_with_replace_so_other_braces_survive():
    assert fill_client('{client_name} says {"a": 1}', "Acme") == 'Acme says {"a": 1}'
    assert DEMO_CLIENT in demo_prompt("for {client_name}")


def test_an_unknown_name_gets_maya_not_an_agent_called_whatever_was_typed():
    assert demo_config("nosuch").persona == DEFAULT_PERSONA
    assert demo_config("nosuch").name == "Maya"


@pytest.mark.parametrize("persona", PERSONAS)
def test_every_persona_speaks_on_edge_tts(persona):
    """The free synthesizer, and the only one the Worker registers."""
    config = demo_config(persona)
    assert config.tts_provider == "edge_tts"
    assert config.voice.endswith("Neural")


def test_kofi_has_his_own_voice():
    assert _PERSONAS["kofi"]["voice"]["voice"] == "en-US-EricNeural"


def test_the_caller_is_never_shown_provider_detail():
    for word in ("Groq", "Cerebras", "401", "Error", "API"):
        assert word not in CALL_ERROR_TEXT


def test_limits_have_defaults_and_read_the_environment(monkeypatch):
    monkeypatch.delenv("VOICE_DEMO_IDLE_S", raising=False)
    monkeypatch.delenv("VOICE_DEMO_CALLS_PER_HOUR", raising=False)
    assert idle_s() == 180
    assert calls_per_hour() == 10
    monkeypatch.setenv("VOICE_DEMO_IDLE_S", "45")
    monkeypatch.setenv("VOICE_DEMO_CALLS_PER_HOUR", "0")
    assert idle_s() == 45
    assert calls_per_hour() == 0
