"""Tests for cynea.api — routes, auth enforcement, and tenant isolation.

Runs against SQLite through FastAPI's TestClient, so no server and no
Postgres are needed. The important cases here are the negative ones:
another workspace's rows must be unreachable even with a valid session
and a correct id.
"""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")

    from cynea import db as dbmod
    dbmod.reset_connection()
    dbmod.init_db()

    from cynea.api import app
    with TestClient(app) as c:
        yield c
    dbmod.reset_connection()


def _account(client, email="owner@example.com"):
    r = client.post("/auth/register", json={"email": email, "password": "demo1234"})
    assert r.status_code == 201, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _agent(client, headers, persona="kwame", name="Front Desk"):
    r = client.post("/agents", headers=headers, json={"name": name, "persona": persona})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ----------------------------------------------------------------------
# System
# ----------------------------------------------------------------------

def test_health_reports_database_and_providers(client):
    body = client.get("/health").json()
    assert body["database"] is True
    assert "groq" in body["providers"]["llm"]


# ----------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------

def test_register_returns_a_usable_token(client):
    headers = _account(client)
    assert client.get("/auth/me", headers=headers).status_code == 200


def test_login_after_register(client):
    _account(client, "ama@example.com")
    r = client.post("/auth/login",
                    json={"email": "ama@example.com", "password": "demo1234"})
    assert r.status_code == 200 and r.json()["token"]


def test_login_with_a_wrong_password_is_401(client):
    _account(client, "ama@example.com")
    r = client.post("/auth/login",
                    json={"email": "ama@example.com", "password": "nope1234"})
    assert r.status_code == 401


def test_duplicate_registration_is_400(client):
    _account(client, "ama@example.com")
    r = client.post("/auth/register",
                    json={"email": "ama@example.com", "password": "demo1234"})
    assert r.status_code == 400


def test_short_password_is_rejected_by_validation(client):
    r = client.post("/auth/register", json={"email": "x@example.com", "password": "abc"})
    assert r.status_code == 422


@pytest.mark.parametrize("path", ["/agents", "/calls", "/dashboard/stats", "/auth/me"])
def test_protected_routes_require_a_token(client, path):
    assert client.get(path).status_code == 401


def test_a_forged_token_is_rejected(client):
    r = client.get("/agents", headers={"Authorization": "Bearer forged.signature"})
    assert r.status_code == 401


# ----------------------------------------------------------------------
# Agents
# ----------------------------------------------------------------------

def test_agent_crud_round_trip(client):
    h = _account(client)
    agent_id = _agent(client, h)

    assert len(client.get("/agents", headers=h).json()) == 1

    got = client.get(f"/agents/{agent_id}", headers=h).json()
    assert got["persona"] == "kwame"
    assert got["voice_config"], "voice config should default from the persona"

    r = client.put(f"/agents/{agent_id}", headers=h, json={"name": "Reception"})
    assert r.status_code == 200 and r.json()["name"] == "Reception"

    assert client.delete(f"/agents/{agent_id}", headers=h).status_code == 204
    assert client.get(f"/agents/{agent_id}", headers=h).status_code == 404


def test_unknown_persona_is_rejected_with_the_valid_list(client):
    h = _account(client)
    r = client.post("/agents", headers=h, json={"name": "X", "persona": "nope"})
    assert r.status_code == 400
    assert "kwame" in r.json()["detail"]


def test_update_with_no_fields_is_400(client):
    h = _account(client)
    agent_id = _agent(client, h)
    assert client.put(f"/agents/{agent_id}", headers=h, json={}).status_code == 400


def test_prompt_versions_increment(client):
    h = _account(client)
    agent_id = _agent(client, h)

    assert client.post(f"/agents/{agent_id}/prompt", headers=h,
                       json={"content": "v1"}).json()["version"] == 1
    assert client.post(f"/agents/{agent_id}/prompt", headers=h,
                       json={"content": "v2"}).json()["version"] == 2

    detail = client.get(f"/agents/{agent_id}", headers=h).json()
    assert detail["prompt"] == "v2" and detail["prompt_version"] == 2


def test_empty_prompt_is_rejected(client):
    h = _account(client)
    agent_id = _agent(client, h)
    r = client.post(f"/agents/{agent_id}/prompt", headers=h, json={"content": "   "})
    assert r.status_code == 400


# ----------------------------------------------------------------------
# Tenant isolation — the cases that matter most
# ----------------------------------------------------------------------

def test_another_users_agent_is_not_readable(client):
    owner = _account(client, "owner@example.com")
    agent_id = _agent(client, owner)
    intruder = _account(client, "intruder@example.com")

    assert client.get(f"/agents/{agent_id}", headers=intruder).status_code == 404


def test_another_users_agent_is_not_writable(client):
    owner = _account(client, "owner@example.com")
    agent_id = _agent(client, owner)
    intruder = _account(client, "intruder@example.com")

    assert client.put(f"/agents/{agent_id}", headers=intruder,
                      json={"name": "hijacked"}).status_code == 404
    assert client.delete(f"/agents/{agent_id}", headers=intruder).status_code == 404


def test_another_users_agent_list_is_empty(client):
    owner = _account(client, "owner@example.com")
    _agent(client, owner)
    intruder = _account(client, "intruder@example.com")
    assert client.get("/agents", headers=intruder).json() == []


def test_cannot_log_a_call_against_another_users_agent(client):
    owner = _account(client, "owner@example.com")
    agent_id = _agent(client, owner)
    intruder = _account(client, "intruder@example.com")

    r = client.post("/calls", headers=intruder,
                    json={"agent_id": agent_id, "caller_number": "+254700000000"})
    assert r.status_code == 404


def test_another_users_call_is_not_readable(client):
    owner = _account(client, "owner@example.com")
    agent_id = _agent(client, owner)
    call_id = client.post("/calls", headers=owner, json={
        "agent_id": agent_id, "caller_number": "+254700000000", "duration_s": 10,
    }).json()["id"]

    intruder = _account(client, "intruder@example.com")
    assert client.get(f"/calls/{call_id}", headers=intruder).status_code == 404


# ----------------------------------------------------------------------
# Calls
# ----------------------------------------------------------------------

def test_log_and_read_a_call(client):
    h = _account(client)
    agent_id = _agent(client, h)

    call_id = client.post("/calls", headers=h, json={
        "agent_id": agent_id, "caller_number": "+233240004417", "duration_s": 79,
        "transcript": "Booking confirmed\n\nCaller: hi\nKwame: booked.",
        "sentiment_score": 0.62, "cost_cents": 5, "status": "resolved",
    }).json()["id"]

    detail = client.get(f"/calls/{call_id}", headers=h).json()
    assert detail["summary"] == "Booking confirmed"
    assert [t["speaker"] for t in detail["turns"]] == ["caller", "agent"]
    assert detail["cost"] == "$0.05"


def test_caller_number_is_masked_in_responses(client):
    h = _account(client)
    agent_id = _agent(client, h)
    client.post("/calls", headers=h, json={
        "agent_id": agent_id, "caller_number": "+233240004417", "duration_s": 10})

    row = client.get("/calls", headers=h).json()[0]
    assert "0004417" not in row["caller"]
    assert row["caller"].endswith("4417")


def test_invalid_call_status_is_rejected(client):
    h = _account(client)
    agent_id = _agent(client, h)
    r = client.post("/calls", headers=h, json={
        "agent_id": agent_id, "caller_number": "+254700000000", "status": "finished"})
    assert r.status_code == 400


def test_out_of_range_sentiment_is_rejected(client):
    h = _account(client)
    agent_id = _agent(client, h)
    r = client.post("/calls", headers=h, json={
        "agent_id": agent_id, "caller_number": "+254700000000", "sentiment_score": 5})
    assert r.status_code == 422


def test_missing_call_is_404(client):
    h = _account(client)
    r = client.get("/calls/00000000-0000-0000-0000-000000000000", headers=h)
    assert r.status_code == 404


# ----------------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------------

def test_dashboard_stats_on_an_empty_workspace(client):
    """A brand new account must not divide by zero."""
    h = _account(client)
    stats = client.get("/dashboard/stats", headers=h).json()
    assert stats["kpis"]["calls_today"] == 0
    assert stats["kpis"]["containment_pct"] == 0
    assert stats["kpis"]["cost_per_call"] == 0


def test_dashboard_stats_counts_real_calls(client):
    h = _account(client)
    agent_id = _agent(client, h)
    for status in ("resolved", "resolved", "escalated"):
        client.post("/calls", headers=h, json={
            "agent_id": agent_id, "caller_number": "+254700000000",
            "duration_s": 60, "cost_cents": 5, "status": status})

    kpis = client.get("/dashboard/stats", headers=h).json()["kpis"]
    assert kpis["calls_today"] == 3
    assert kpis["containment_pct"] == 67          # 2 of 3
    assert kpis["cost_today_cents"] == 15


def test_bootstrap_returns_every_section(client):
    h = _account(client)
    body = client.get("/dashboard/bootstrap", headers=h).json()
    assert set(body) == {"stats", "queue", "calls"}


def test_dashboard_only_sees_its_own_workspace(client):
    owner = _account(client, "owner@example.com")
    agent_id = _agent(client, owner)
    client.post("/calls", headers=owner, json={
        "agent_id": agent_id, "caller_number": "+254700000000", "duration_s": 60})

    intruder = _account(client, "intruder@example.com")
    assert client.get("/dashboard/stats", headers=intruder).json()["kpis"]["calls_today"] == 0
