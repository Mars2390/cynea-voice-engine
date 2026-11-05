"""A customer's call in the database: what the engine writes, and what comes back.

The browser demo never writes a row, so this is the code path a real call would
use. It runs on SQLite here. tests/test_postgres.py covers what differs on the
database production uses.
"""

from __future__ import annotations

import asyncio

import pytest

from cynea import db, providers
from cynea.engine import CyneaEngine
from cynea.models import AgentConfig, AudioChunk, Transcription


class STT:
    async def transcribe(self, audio):
        return Transcription(text="I would like a table", confidence=1.0, is_final=True)


class LLM:
    async def generate(self, messages, system=""):
        return "Sure, a table for you."


class TTS:
    async def synthesize(self, request):
        return b"mp3"


AUDIO = AudioChunk(data=b"\0\1" * 16000, sample_rate=16000, channels=1, encoding="pcm16")


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p.db'}")
    db.reset_connection()
    db.init_db()
    providers.register_stt("p_stt", STT)
    providers.register_llm("p_llm", LLM)
    providers.register_tts("p_tts", TTS)
    user = db.create_user("a@example.com", "x")
    yield db.create_agent(user.id, "Front Desk", "kwame")
    db.reset_connection()


def engine(agent_id=None, persist=True):
    config = AgentConfig(name="Front Desk", system_prompt="sys", first_message="Hello.",
                         stt_provider="p_stt", llm_provider="p_llm", tts_provider="p_tts")
    return CyneaEngine(config, agent_id=agent_id, persist=persist)


def run_call(e, turns=1):
    async def go():
        await e.start()
        for _ in range(turns):
            await e.process_audio(AUDIO)
    asyncio.run(go())


def test_a_call_gets_a_row_on_its_first_turn(agent):
    e = engine(agent.id)
    run_call(e)
    assert e.call_id
    row = db.get_call_by_id(e.call_id)
    assert row.agent_id == agent.id
    assert "I would like a table" in row.transcript


def test_until_it_ends_a_row_reads_as_abandoned(agent):
    e = engine(agent.id)
    run_call(e)
    assert db.get_call_by_id(e.call_id).status == "abandoned"


def test_ending_a_call_resolves_it_and_keeps_the_transcript(agent):
    e = engine(agent.id)
    run_call(e, turns=2)
    e.end_call()
    row = db.get_call_by_id(e.call_id)
    assert row.status == "resolved"
    assert row.transcript.count("I would like a table") == 2
    assert row.duration_s >= 0 and row.cost_cents >= 0


def test_an_escalated_call_says_so(agent):
    e = engine(agent.id)
    run_call(e)
    e.end_call(escalated=True)
    assert db.get_call_by_id(e.call_id).status == "escalated"


def test_only_one_row_per_call(agent):
    e = engine(agent.id)
    run_call(e, turns=3)
    e.end_call()
    assert len(db.get_calls_by_agent(agent.id)) == 1


@pytest.mark.parametrize("kwargs", [{"agent_id": None}, {"persist": False}])
def test_no_agent_or_persist_off_writes_nothing(agent, kwargs):
    e = engine(**{"agent_id": agent.id, **kwargs})
    run_call(e)
    assert e.call_id is None and db.get_calls_by_agent(agent.id) == []


def test_a_database_failure_does_not_stop_the_call(agent, monkeypatch):
    """A caller on the line matters more than the row about them."""
    e = engine(agent.id)

    def down(*a, **k):
        raise RuntimeError("database gone")
    monkeypatch.setattr(db, "log_call", down)

    async def go():
        await e.start()
        return await e.process_audio(AUDIO)
    assert str(asyncio.run(go())) == "Sure, a table for you."


def test_deleting_an_agent_takes_its_calls(agent):
    e = engine(agent.id)
    run_call(e)
    db.delete_agent(agent.id)
    assert db.get_call_by_id(e.call_id) is None
