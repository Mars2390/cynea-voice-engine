"""What behaves differently on Postgres, the database production runs.

Skipped unless TEST_POSTGRES_URL is set. CI sets it against a throwaway postgres:16
service. Locally:

    docker run -d -e POSTGRES_PASSWORD=x -p 5432:5432 postgres:16
    TEST_POSTGRES_URL=postgresql://postgres:x@localhost:5432/postgres pytest tests/test_postgres.py

Every table is dropped at the start and end of each test, so never point this at
anything you want to keep. It refuses a Neon host for that reason.

The differences between SQLite and Postgres that matter here
------------------------------------------------------------
Malformed uuid       SQLite: finds nothing. Postgres: raises DataError, a 500 if
                     unguarded.
Timestamps           SQLite: naive datetimes, despite timezone=True. Postgres: aware.
                     Subtracting a naive one from an aware one raises.
ON DELETE CASCADE    SQLite: ignored unless asked for per connection. Postgres:
                     enforced by the database as well as by the ORM.
FK violations        SQLite: not enforced by default. Postgres: raises IntegrityError.
Unique violations    Both raise, and Postgres also aborts the transaction.
UUID columns         SQLite: CHAR(36). Postgres: native uuid. A comparison against a
                     non-uuid string fails on Postgres.
GROUP BY             SQLite lets you select an ungrouped column. Postgres refuses.
                     Only a real Postgres catches this in the dashboard's queries.
JSON                 SQLite: text. Postgres: json. Both round-trip through the ORM.
Adding columns       upgrade_schema's ALTER TABLE has to compile for each dialect.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import pytest

URL = os.getenv("TEST_POSTGRES_URL", "").strip()

pytestmark = pytest.mark.skipif(not URL, reason="TEST_POSTGRES_URL is not set")


@pytest.fixture()
def pg(monkeypatch):
    if "neon.tech" in URL:
        pytest.fail("TEST_POSTGRES_URL points at Neon. These tests drop every table.")
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("EMAIL_DEV_LOG_LINKS", "1")
    from cynea import db
    db.reset_connection()
    db.Base.metadata.drop_all(db.get_engine())
    db.init_db()
    yield db
    db.Base.metadata.drop_all(db.get_engine())
    db.reset_connection()


@pytest.fixture()
def api(pg, monkeypatch):
    from fastapi.testclient import TestClient
    from cynea import mailer
    from cynea.api import app

    links = {}
    monkeypatch.setattr(mailer, "send_verification",
                        lambda to, link, name="": links.__setitem__(to, link) or "logged")
    monkeypatch.setattr(mailer, "send_password_reset",
                        lambda to, link: links.__setitem__(to, link) or "logged")
    with TestClient(app) as c:
        c.post("/auth/register", json={"email": "a@example.com", "password": "correct-horse-1"})
        link = links["a@example.com"]
        c.get(link[link.index("/auth/verify"):], follow_redirects=False)
        pg.set_user_status(pg.get_user_by_email("a@example.com").id, pg.APPROVED)
        token = c.post("/auth/login", json={"email": "a@example.com",
                                            "password": "correct-horse-1"}).json()["token"]
        c.h = {"Authorization": f"Bearer {token}"}
        c.links = links
        yield c


def _agent(c, name="Front Desk", persona="kwame"):
    r = c.post("/agents", headers=c.h, json={"name": name, "persona": persona})
    assert r.status_code == 201, r.text
    return r.json()


def _call(c, agent, **fields):
    r = c.post("/calls", headers=c.h, json={"agent_id": agent["id"], "caller_number": "+15550100",
                                            **fields})
    assert r.status_code == 201, r.text
    return r.json()


# ── uuid columns ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["/agents/not-a-uuid", "/calls/not-a-uuid",
                                  "/agents/not-a-uuid/prompts", "/calls/not-a-uuid/recording"])
def test_a_malformed_id_is_a_404_not_a_database_error(api, path):
    assert api.get(path, headers=api.h).status_code == 404


def test_call_with_a_malformed_agent_id_is_a_404(api):
    r = api.post("/calls", headers=api.h, json={"agent_id": "nope", "caller_number": "+1"})
    assert r.status_code == 404


def test_a_ticket_with_a_malformed_agent_is_refused(api):
    from cynea import auth
    ticket = auth.sign_value("call", {"agent": "nope", "user": "nope"}, 60)
    assert api.get("/call-config", params={"ticket": ticket}).status_code == 403


def test_a_session_for_a_malformed_user_is_a_401(api):
    from cynea import auth
    token = auth.create_session("not-a-uuid")
    assert api.get("/agents", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_revoking_a_malformed_key_id_is_a_404(api):
    assert api.delete("/api-keys/nope", headers=api.h).status_code == 404
    assert api.delete("/webhooks/nope", headers=api.h).status_code == 404


# ── timestamps ───────────────────────────────────────────────────────────
def test_timestamps_come_back_aware(pg):
    user = pg.create_user("b@example.com", "x")
    assert pg.get_user_by_id(user.id).created_at.utcoffset() is not None


def test_the_queue_works_on_aware_timestamps(api):
    agent = _agent(api)
    _call(api, agent, status="abandoned")
    rows = api.get("/dashboard/queue", headers=api.h).json()
    assert len(rows) == 1 and rows[0]["age_s"] >= 0
    assert datetime.fromisoformat(rows[0]["created_at"]).utcoffset() is not None


def test_a_reset_link_expires_on_aware_timestamps(api, pg):
    api.post("/auth/forgot-password", json={"email": "a@example.com"})
    token = re.search(r"reset=(\S+)", api.links["a@example.com"]).group(1)
    with pg.session_scope() as s:
        s.query(pg.PasswordReset).update(
            {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)})
    r = api.post("/auth/reset-password", json={"token": token, "password": "brand-new-pass-1"})
    assert r.status_code == 400


def test_a_verification_link_expires_on_aware_timestamps(pg):
    from cynea import auth
    user = pg.create_user("b@example.com", "x")
    raw = auth.issue_verification(user.id)
    with pg.session_scope() as s:
        s.get(pg.User, user.id).verify_token_expires = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert auth.verify_email(raw) is None


def test_api_key_last_used_compares_on_aware_timestamps(api):
    raw = api.post("/api-keys", headers=api.h, json={"name": "ci"}).json()["key"]
    h = {"Authorization": f"Bearer {raw}"}
    assert api.get("/agents", headers=h).status_code == 200
    assert api.get("/agents", headers=h).status_code == 200
    assert api.get("/api-keys", headers=api.h).json()[0]["last_used_at"]


# ── aggregates, which only a real Postgres checks ────────────────────────
def test_the_dashboards_queries_run_and_add_up(api):
    a, b = _agent(api, "Front Desk"), _agent(api, "Night Desk")
    _call(api, a, sentiment_score=1.0, cost_cents=10, status="resolved")
    _call(api, a, sentiment_score=1.0, cost_cents=10, status="resolved")
    _call(api, a, sentiment_score=1.0, cost_cents=10, status="resolved")
    _call(api, b, sentiment_score=-1.0, cost_cents=30, status="escalated")

    stats = api.get("/dashboard/stats", headers=api.h, params={"tz_offset_min": 330}).json()
    assert stats["kpis"]["calls_today"] == 4
    assert stats["kpis"]["avg_sentiment"] == 0.5
    assert stats["kpis"]["cost_today_cents"] == 60
    assert sum(row["v"] for row in stats["volume"]) == 4
    assert {row["k"]: row["calls"] for row in stats["cost_breakdown"]} == {"Front Desk": 3, "Night Desk": 1}
    assert stats["totals"]["week_calls"] == 4


def test_bootstrap_runs(api):
    _call(api, _agent(api))
    r = api.get("/dashboard/bootstrap", headers=api.h, params={"tz_offset_min": -300})
    assert r.status_code == 200 and len(r.json()["calls"]) == 1


def test_two_agents_with_one_name_stay_separate_in_the_breakdown(api):
    a, b = _agent(api, "Desk"), _agent(api, "Desk")
    _call(api, a, cost_cents=1)
    _call(api, b, cost_cents=2)
    rows = api.get("/dashboard/stats", headers=api.h).json()["cost_breakdown"]
    assert {r["agent_id"] for r in rows} == {a["id"], b["id"]}


# ── constraints the database itself enforces ─────────────────────────────
def test_a_call_cannot_point_at_a_missing_agent(pg):
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        pg.log_call("00000000-0000-0000-0000-000000000000", "+1", 1)


def test_a_duplicate_email_is_refused_and_the_session_recovers(pg):
    from sqlalchemy.exc import IntegrityError
    pg.create_user("b@example.com", "x")
    with pytest.raises(IntegrityError):
        pg.create_user("B@example.com ", "x")
    assert pg.get_user_by_email("b@example.com") is not None


def test_the_database_cascades_even_without_the_orm(pg):
    from sqlalchemy import func, select, text
    user = pg.create_user("b@example.com", "x")
    agent = pg.create_agent(user.id, "Desk", "kwame")
    pg.log_call(agent.id, "+1", 1)
    pg.save_prompt_version(agent.id, "hi")
    with pg.get_engine().begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
    with pg.session_scope() as s:
        for model in (pg.Agent, pg.Call, pg.PromptVersion):
            assert s.scalar(select(func.count()).select_from(model)) == 0


def test_deleting_an_account_through_the_api_leaves_nothing(api, pg):
    from sqlalchemy import func, select
    _call(api, _agent(api))
    api.post("/api-keys", headers=api.h, json={"name": "ci"})
    api.post("/auth/forgot-password", json={"email": "a@example.com"})
    r = api.request("DELETE", "/auth/me", headers=api.h,
                    json={"password": "correct-horse-1", "confirm": "DELETE"})
    assert r.status_code == 204
    with pg.session_scope() as s:
        for model in (pg.User, pg.Agent, pg.Call, pg.ApiKey, pg.PasswordReset, pg.SessionCutoff):
            assert s.scalar(select(func.count()).select_from(model)) == 0, model


def test_voice_config_round_trips_as_json(api):
    made = api.post("/agents", headers=api.h, json={
        "name": "Desk", "persona": "amina",
        "voice_config": {"voice": "en-NG-AbeoNeural", "speed": 1.25}}).json()
    got = api.get(f"/agents/{made['id']}", headers=api.h).json()
    assert got["voice_config"]["speed"] == 1.25
    assert api.put(f"/agents/{made['id']}", headers=api.h, json={"paused": True}).json()["paused"] is True


def test_rate_limits_count_across_connections(pg):
    """The point of counting in the database: a new instance sees the same rows."""
    from cynea import ratelimit
    for _ in range(3):
        ratelimit.record("login", "1.2.3.4|a@example.com")
    pg.reset_connection()
    assert ratelimit.over("login", "1.2.3.4|a@example.com", 3, 900) is True
    assert ratelimit.over("login", "1.2.3.4|b@example.com", 3, 900) is False


def test_logout_and_cutoff_work_with_native_uuid_keys(api):
    other = api.post("/auth/login", json={"email": "a@example.com",
                                          "password": "correct-horse-1"}).json()["token"]
    other_h = {"Authorization": f"Bearer {other}"}
    assert api.post("/auth/logout", headers=api.h).status_code == 204
    assert api.get("/agents", headers=api.h).status_code == 401
    assert api.get("/agents", headers=other_h).status_code == 200
    r = api.post("/auth/change-password", headers=other_h,
                 json={"current_password": "correct-horse-1", "new_password": "brand-new-pass-1"})
    assert r.status_code == 200
    assert api.get("/agents", headers=other_h).status_code == 401


# ── migrating a live table ───────────────────────────────────────────────
def test_upgrade_schema_adds_a_missing_column_on_postgres(pg):
    from sqlalchemy import inspect, text
    with pg.get_engine().begin() as conn:
        conn.execute(text("ALTER TABLE calls DROP COLUMN audio_url"))
    assert pg.upgrade_schema() == ["calls.audio_url"]
    assert "audio_url" in {c["name"] for c in inspect(pg.get_engine()).get_columns("calls")}


def test_upgrade_schema_is_safe_to_run_twice(pg):
    assert pg.upgrade_schema() == []
    assert pg.upgrade_schema() == []


def test_sign_in_survives_tables_that_are_not_migrated_yet(api, pg):
    from sqlalchemy import text
    with pg.get_engine().begin() as conn:
        conn.execute(text("DROP TABLE revoked_tokens"))
        conn.execute(text("DROP TABLE session_cutoffs"))
    pg._READY_TABLES.clear()
    assert api.get("/agents", headers=api.h).status_code == 200
