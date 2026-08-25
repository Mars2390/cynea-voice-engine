"""Tests for cynea.db — schema, CRUD, cascades, and validation.

Runs against SQLite via a temp file, so the suite needs no Postgres. The
GUID column and JSON type are both dialect-portable for exactly this
reason; the same code paths run against Neon in production.
"""

import os
import uuid

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Fresh database per test."""
    from cynea import db as dbmod

    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    dbmod.reset_connection()
    dbmod.init_db()
    yield dbmod
    dbmod.reset_connection()


@pytest.fixture()
def user(db):
    return db.create_user("ama@adinkra.example", "hash-1")


@pytest.fixture()
def agent(db, user):
    return db.create_agent(user.id, "Front Desk", "kwame",
                           {"provider": "edge_tts", "voice": "en-GB-RyanNeural"})


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

def test_missing_database_url_raises_with_setup_instructions(monkeypatch):
    from cynea import db as dbmod
    monkeypatch.delenv("DATABASE_URL", raising=False)
    dbmod.reset_connection()
    with pytest.raises(dbmod.DatabaseNotConfigured) as info:
        dbmod.get_database_url()
    assert "neon.tech" in str(info.value)


def test_legacy_postgres_scheme_is_normalised(monkeypatch):
    """Some hosts still emit postgres://, which SQLAlchemy 2.x rejects."""
    from cynea import db as dbmod
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@host/db")
    assert dbmod.get_database_url().startswith("postgresql://")


def test_healthcheck_true_when_reachable(db):
    assert db.healthcheck() is True


def test_init_db_is_idempotent(db):
    db.init_db()
    db.init_db()   # must not raise


# ----------------------------------------------------------------------
# Users
# ----------------------------------------------------------------------

def test_create_and_fetch_user(db):
    created = db.create_user("Kofi@Example.COM", "hashed")
    assert uuid.UUID(created.id)
    found = db.get_user_by_email("kofi@example.com")
    assert found is not None and found.id == created.id


def test_email_is_stored_lowercase(db):
    db.create_user("MAYA@Example.com", "h")
    assert db.get_user_by_email("maya@example.com") is not None


def test_duplicate_email_is_rejected(db):
    db.create_user("dup@example.com", "h")
    with pytest.raises(Exception):
        db.create_user("dup@example.com", "h2")


def test_unknown_email_returns_none(db):
    assert db.get_user_by_email("nobody@example.com") is None


# ----------------------------------------------------------------------
# Agents
# ----------------------------------------------------------------------

def test_create_agent_stores_voice_config_as_json(db, agent):
    fetched = db.get_agent_by_id(agent.id)
    assert fetched.voice_config["voice"] == "en-GB-RyanNeural"


def test_get_agents_by_user(db, user, agent):
    db.create_agent(user.id, "Bookings", "maya", {})
    agents = db.get_agents_by_user(user.id)
    assert {a.persona for a in agents} == {"kwame", "maya"}


def test_update_agent_changes_fields(db, agent):
    updated = db.update_agent(agent.id, name="Reception", persona="AMINA")
    assert updated.name == "Reception"
    assert updated.persona == "amina", "persona should be normalised to lowercase"


def test_update_agent_rejects_unknown_fields(db, agent):
    """A typo should fail loudly, not silently do nothing."""
    with pytest.raises(ValueError) as info:
        db.update_agent(agent.id, nmae="typo")
    assert "nmae" in str(info.value)


def test_update_missing_agent_returns_none(db):
    assert db.update_agent(str(uuid.uuid4()), name="x") is None


def test_delete_agent(db, agent):
    assert db.delete_agent(agent.id) is True
    assert db.get_agent_by_id(agent.id) is None


def test_delete_missing_agent_returns_false(db):
    assert db.delete_agent(str(uuid.uuid4())) is False


# ----------------------------------------------------------------------
# Calls
# ----------------------------------------------------------------------

def test_log_and_fetch_call(db, agent):
    call = db.log_call(agent.id, "+233 24 000 4417", 79,
                       transcript="Caller asked about a double room.",
                       sentiment=0.62, cost=5, status="resolved")
    fetched = db.get_call_by_id(call.id)
    assert fetched.duration_s == 79
    assert fetched.cost_cents == 5
    assert fetched.sentiment_score == pytest.approx(0.62)


def test_calls_come_back_newest_first(db, agent):
    for i in range(3):
        db.log_call(agent.id, f"+2547000000{i}", i)
    calls = db.get_calls_by_agent(agent.id)
    assert [c.duration_s for c in calls] == [2, 1, 0]


def test_calls_can_be_filtered_by_status(db, agent):
    db.log_call(agent.id, "+254700000001", 10, status="resolved")
    db.log_call(agent.id, "+254700000002", 20, status="escalated")
    escalated = db.get_calls_by_agent(agent.id, status="escalated")
    assert len(escalated) == 1 and escalated[0].duration_s == 20


def test_invalid_status_is_rejected(db, agent):
    with pytest.raises(ValueError) as info:
        db.log_call(agent.id, "+254700000000", 10, status="finished")
    assert "resolved" in str(info.value)


@pytest.mark.parametrize("bad", [-1.5, 2.0])
def test_sentiment_outside_minus_one_to_one_is_rejected(db, agent, bad):
    with pytest.raises(ValueError):
        db.log_call(agent.id, "+254700000000", 10, sentiment=bad)


def test_sentiment_may_be_none_for_very_short_calls(db, agent):
    call = db.log_call(agent.id, "+254700000000", 3, sentiment=None)
    assert db.get_call_by_id(call.id).sentiment_score is None


def test_cost_is_stored_as_integer_cents(db, agent):
    """Money must never be a float."""
    call = db.log_call(agent.id, "+254700000000", 60, cost=11)
    assert isinstance(db.get_call_by_id(call.id).cost_cents, int)


# ----------------------------------------------------------------------
# Prompt versions
# ----------------------------------------------------------------------

def test_prompt_versions_number_themselves(db, agent):
    assert db.save_prompt_version(agent.id, "v one").version == 1
    assert db.save_prompt_version(agent.id, "v two").version == 2
    assert db.save_prompt_version(agent.id, "v three").version == 3


def test_prompt_versions_come_back_newest_first(db, agent):
    for text in ("a", "b", "c"):
        db.save_prompt_version(agent.id, text)
    assert [v.content for v in db.get_prompt_versions(agent.id)] == ["c", "b", "a"]


def test_get_latest_prompt(db, agent):
    db.save_prompt_version(agent.id, "old")
    db.save_prompt_version(agent.id, "current")
    assert db.get_latest_prompt(agent.id).content == "current"


def test_latest_prompt_is_none_when_there_are_none(db, agent):
    assert db.get_latest_prompt(agent.id) is None


def test_version_numbering_is_per_agent(db, user, agent):
    other = db.create_agent(user.id, "Second", "kofi", {})
    db.save_prompt_version(agent.id, "a")
    db.save_prompt_version(agent.id, "b")
    assert db.save_prompt_version(other.id, "first for other").version == 1


# ----------------------------------------------------------------------
# Cascades — deleting must not leave orphans
# ----------------------------------------------------------------------

def test_deleting_an_agent_removes_its_calls_and_prompts(db, agent):
    call = db.log_call(agent.id, "+254700000000", 10)
    version = db.save_prompt_version(agent.id, "prompt")

    db.delete_agent(agent.id)

    assert db.get_call_by_id(call.id) is None
    assert db.get_prompt_versions(agent.id) == []


def test_deleting_a_user_removes_their_agents(db, user, agent):
    with db.session_scope() as s:
        s.delete(s.get(db.User, user.id))
    assert db.get_agent_by_id(agent.id) is None
