"""Accounts and integrations, through the API, on SQLite, with no network.

Sign-in limits, sign-out, password recovery, deleting and exporting an account,
API keys, webhooks, test-call tickets, and the numbers the dashboard reports.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

EMAIL, PASSWORD = "ama@example.com", "correct-horse-1"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'a.db'}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("EMAIL_DEV_LOG_LINKS", "1")
    for k in ("EMAIL_API_KEY", "TURNSTILE_SECRET_KEY", "TURNSTILE_SITE_KEY", "ENV", "VERCEL",
              "SITE_URL", "PUBLIC_API_URL", "VERCEL_PROJECT_PRODUCTION_URL", "SENTRY_DSN",
              "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET",
              "WEBHOOK_ALLOW_PRIVATE"):
        monkeypatch.delenv(k, raising=False)

    from cynea import db, mailer
    verify_links, reset_links = {}, {}
    monkeypatch.setattr(mailer, "send_verification",
                        lambda to, link, name="": verify_links.__setitem__(to, link) or "logged")
    monkeypatch.setattr(mailer, "send_password_reset",
                        lambda to, link: reset_links.__setitem__(to, link) or "logged")
    db.reset_connection()
    db.init_db()
    from cynea.api import app
    with TestClient(app) as c:
        c.verify_links, c.reset_links = verify_links, reset_links
        yield c
    db.reset_connection()


def _approve(email=EMAIL):
    """What an administrator does on admin.html."""
    from cynea import db
    db.set_user_status(db.get_user_by_email(email).id, db.APPROVED)


def _sign_up(c, email=EMAIL, password=PASSWORD, verify=True, **extra):
    """Register, then (unless verify=False) follow the link and get approved."""
    r = c.post("/auth/register", json={"email": email, "password": password, **extra})
    assert r.status_code == 201, r.text
    if verify:
        link = c.verify_links[email]
        c.get(link[link.index("/auth/verify"):], follow_redirects=False)
        _approve(email)


def _login(c, email=EMAIL, password=PASSWORD):
    r = c.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def signed_in(env):
    _sign_up(env)
    return env, _login(env)


def _reset_token(c, email=EMAIL):
    return re.search(r"reset=([^&\s]+)", c.reset_links[email]).group(1)


def _agent(c, h, name="Front Desk", persona="kwame", **extra):
    r = c.post("/agents", headers=h, json={"name": name, "persona": persona, **extra})
    assert r.status_code == 201, r.text
    return r.json()


# ── sign-in limit ────────────────────────────────────────────────────────
def test_login_is_limited_after_repeated_failures(env):
    _sign_up(env)
    for _ in range(10):
        assert env.post("/auth/login", json={"email": EMAIL, "password": "wrong-wrong"}).status_code == 401
    assert env.post("/auth/login", json={"email": EMAIL, "password": "wrong-wrong"}).status_code == 429
    assert env.post("/auth/login", json={"email": EMAIL, "password": PASSWORD}).status_code == 429


def test_the_count_is_in_the_database_not_this_process(env):
    """A new serverless instance must see the same failures."""
    from cynea import db
    from sqlalchemy import func, select
    _sign_up(env)
    env.post("/auth/login", json={"email": EMAIL, "password": "wrong-wrong"})
    with db.session_scope() as s:
        assert s.scalar(select(func.count(db.RateEvent.id))
                        .where(db.RateEvent.scope == "login")) == 1


def test_good_sign_ins_use_up_no_allowance(env):
    _sign_up(env)
    for _ in range(12):
        _login(env)


def test_failing_on_one_address_does_not_lock_out_another(env):
    _sign_up(env)
    _sign_up(env, email="kofi@example.com")
    for _ in range(11):
        env.post("/auth/login", json={"email": EMAIL, "password": "wrong-wrong"})
    _login(env, "kofi@example.com")


# ── signing out ──────────────────────────────────────────────────────────
def test_logout_invalidates_the_session_on_the_server(signed_in):
    c, h = signed_in
    assert c.get("/auth/me", headers=h).status_code == 200
    assert c.post("/auth/logout", headers=h).status_code == 204
    assert c.get("/auth/me", headers=h).status_code == 401
    assert c.get("/agents", headers=h).status_code == 401


def test_logout_only_ends_that_session(signed_in):
    c, h = signed_in
    other = _login(c)
    c.post("/auth/logout", headers=h)
    assert c.get("/auth/me", headers=other).status_code == 200


def test_a_deleted_accounts_token_stops_working_everywhere(signed_in):
    from cynea import db
    c, h = signed_in
    db.delete_user(db.get_user_by_email(EMAIL).id)
    assert c.get("/agents", headers=h).status_code == 401


@pytest.mark.parametrize("token", ["ünïcode.ünïcode", "abc.ünï", "not-a-token", "a.b.c"])
def test_a_garbage_token_is_a_401_not_a_crash(env, token):
    r = env.get("/agents", headers={"Authorization": b"Bearer " + token.encode("latin-1", "replace")})
    assert r.status_code == 401


# ── password recovery ────────────────────────────────────────────────────
def test_forgot_password_gives_the_same_answer_either_way(env):
    _sign_up(env)
    known = env.post("/auth/forgot-password", json={"email": EMAIL})
    unknown = env.post("/auth/forgot-password", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert list(env.reset_links) == [EMAIL]


def test_reset_sets_the_password_and_ends_every_session(signed_in):
    c, h = signed_in
    c.post("/auth/forgot-password", json={"email": EMAIL})
    r = c.post("/auth/reset-password", json={"token": _reset_token(c), "password": "brand-new-pass-1"})
    assert r.status_code == 200
    assert c.get("/auth/me", headers=h).status_code == 401
    assert c.post("/auth/login", json={"email": EMAIL, "password": PASSWORD}).status_code == 401
    _login(c, password="brand-new-pass-1")


def test_a_reset_link_works_once(env):
    _sign_up(env)
    env.post("/auth/forgot-password", json={"email": EMAIL})
    token = _reset_token(env)
    assert env.post("/auth/reset-password", json={"token": token, "password": "brand-new-pass-1"}).status_code == 200
    assert env.post("/auth/reset-password", json={"token": token, "password": "brand-new-pass-2"}).status_code == 400


def test_an_expired_reset_link_is_refused(env):
    from cynea import db
    _sign_up(env)
    env.post("/auth/forgot-password", json={"email": EMAIL})
    with db.session_scope() as s:
        s.query(db.PasswordReset).update({"expires_at": db._now() - timedelta(minutes=1)})
    assert env.post("/auth/reset-password",
                    json={"token": _reset_token(env), "password": "brand-new-pass-1"}).status_code == 400


def test_only_a_links_hash_is_stored(env):
    from cynea import db
    from sqlalchemy import select
    _sign_up(env)
    env.post("/auth/forgot-password", json={"email": EMAIL})
    token = _reset_token(env)
    with db.session_scope() as s:
        stored = s.scalar(select(db.PasswordReset.token_hash))
    assert token not in stored and stored == hashlib.sha256(token.encode()).hexdigest()


def test_reset_recovers_an_address_someone_else_squatted(env):
    """Someone registers your email and never verifies. You hold the mailbox."""
    _sign_up(env, verify=False, password="squatters-pass-1", name="Squatter", company="Squat Ltd")
    assert env.post("/auth/login", json={"email": EMAIL, "password": "squatters-pass-1"}).status_code == 403

    env.post("/auth/forgot-password", json={"email": EMAIL})
    env.post("/auth/reset-password", json={"token": _reset_token(env), "password": "brand-new-pass-1"})

    assert env.post("/auth/login", json={"email": EMAIL, "password": "squatters-pass-1"}).status_code == 401
    _approve()                       # the recovered account still goes through review
    me = env.get("/auth/me", headers=_login(env, password="brand-new-pass-1")).json()
    assert me["name"] is None and me["company"] is None


def test_change_password_needs_the_current_one(signed_in):
    c, h = signed_in
    r = c.post("/auth/change-password", headers=h,
               json={"current_password": "not-it-at-all", "new_password": "brand-new-pass-1"})
    assert r.status_code == 403
    assert "current password" in r.json()["detail"]
    assert c.get("/auth/me", headers=h).status_code == 200      # the session is still fine
    _login(c)


def test_change_password_ends_other_sessions_and_keeps_this_tab(signed_in):
    c, h = signed_in
    other = _login(c)
    r = c.post("/auth/change-password", headers=h,
               json={"current_password": PASSWORD, "new_password": "brand-new-pass-1"})
    assert r.status_code == 200
    assert c.get("/auth/me", headers=other).status_code == 401
    assert c.get("/auth/me", headers=h).status_code == 401
    fresh = {"Authorization": f"Bearer {r.json()['token']}"}
    assert c.get("/auth/me", headers=fresh).status_code == 200


# ── links come from configuration, and tokens stay out of logs ───────────
def test_a_forged_host_header_cannot_steer_a_link(env, monkeypatch):
    monkeypatch.setenv("SITE_URL", "https://site.example")
    _sign_up(env)
    env.post("/auth/forgot-password", json={"email": EMAIL}, headers={"Host": "evil.example"})
    assert env.reset_links[EMAIL].startswith("https://site.example/signin.html?reset=")
    env.post("/auth/register", json={"email": "b@example.com", "password": PASSWORD},
             headers={"Host": "evil.example"})
    assert "evil.example" not in env.verify_links["b@example.com"]


def test_production_with_no_address_refuses_rather_than_guessing(env, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("EMAIL_API_KEY", "x")
    r = env.post("/auth/register", json={"email": "b@example.com", "password": PASSWORD})
    assert r.status_code == 503
    from cynea import db
    assert db.get_user_by_email("b@example.com") is None


def test_vercels_own_domain_is_used_before_site_url_is_set(env, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("EMAIL_API_KEY", "x")
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "cynea.example")
    env.post("/auth/register", json={"email": "b@example.com", "password": PASSWORD})
    assert env.verify_links["b@example.com"].startswith("https://cynea.example/api/auth/verify?")


def test_sign_up_without_email_creates_a_pending_account(env, monkeypatch):
    """Replaces the old refusal: with no email, the account waits for an admin."""
    from cynea import db
    monkeypatch.delenv("EMAIL_DEV_LOG_LINKS")
    r = env.post("/auth/register", json={"email": "b@example.com", "password": PASSWORD})
    assert r.status_code == 201
    assert r.json()["message"] == "Account created. An administrator will review it shortly."
    assert db.user_status(db.get_user_by_email("b@example.com")) == db.PENDING
    assert "b@example.com" not in env.verify_links          # nothing was mailed


def test_dev_links_are_refused_in_production(monkeypatch):
    from cynea import mailer
    monkeypatch.delenv("EMAIL_API_KEY", raising=False)
    monkeypatch.setenv("EMAIL_DEV_LOG_LINKS", "1")
    monkeypatch.setenv("ENV", "production")
    assert mailer.can_send() is False
    with pytest.raises(mailer.EmailError):
        mailer.send_password_reset("a@example.com", "https://x/?reset=secret-token")


def test_a_link_is_never_logged(monkeypatch, caplog, capsys):
    from cynea import mailer
    monkeypatch.delenv("EMAIL_DEV_LOG_LINKS", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setenv("EMAIL_API_KEY", "re_test")
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, request=httpx.Request("POST", "http://x")))
    with caplog.at_level(logging.DEBUG, logger="cynea.mailer"):
        mailer.send_password_reset("a@example.com", "https://x/?reset=secret-token")
        mailer.send_verification("a@example.com", "https://x/?token=secret-token")
    out = capsys.readouterr()
    assert "secret-token" not in caplog.text + out.out + out.err
    assert "link generated for a@example.com" in caplog.text


@pytest.mark.parametrize("email", ["plain", "a@b", "two@@x.com", "sp ace@x.com", "a@.com", "a@b."])
def test_bad_addresses_are_refused(env, email):
    assert env.post("/auth/register", json={"email": email, "password": PASSWORD}).status_code == 400


# ── export and delete ────────────────────────────────────────────────────
def _fill(c, h):
    agent = _agent(c, h)
    c.post(f"/agents/{agent['id']}/prompt", headers=h, json={"content": "Be brief."})
    call = c.post("/calls", headers=h, json={
        "agent_id": agent["id"], "caller_number": "+15550100", "duration_s": 3,
        "transcript": "Booked a suite"}).json()
    r = c.post(f"/calls/{call['id']}/recording", headers=h,
               files={"file": ("a.mp3", b"ID3audio", "audio/mpeg")})
    assert r.status_code == 201
    return agent, call


def test_export_has_the_customers_data_and_none_of_the_secrets(signed_in):
    c, h = signed_in
    agent, call = _fill(c, h)
    key = c.post("/api-keys", headers=h, json={"name": "ci"}).json()
    r = c.get("/auth/me/export", headers=h)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert data["account"]["email"] == EMAIL
    assert data["agents"][0]["prompt_versions"][0]["content"] == "Be brief."
    exported = data["agents"][0]["calls"][0]
    assert exported["caller_number"] == "+15550100" and exported["has_recording"] is True
    text = r.text
    assert key["key"] not in text
    for word in ("password_hash", "key_hash", "whsec_", "$2b$"):
        assert word not in text


def test_delete_needs_the_password_and_the_word(signed_in):
    from cynea import db
    c, h = signed_in
    r = c.request("DELETE", "/auth/me", headers=h, json={"password": "not-it-at-all", "confirm": "DELETE"})
    assert r.status_code == 403
    r = c.request("DELETE", "/auth/me", headers=h, json={"password": PASSWORD, "confirm": "yes"})
    assert r.status_code == 400
    assert db.get_user_by_email(EMAIL) is not None


def test_delete_removes_everything_including_the_audio(signed_in, tmp_path):
    from cynea import db
    from sqlalchemy import func, select
    c, h = signed_in
    _fill(c, h)
    c.post("/api-keys", headers=h, json={"name": "ci"})
    audio = list((tmp_path / "recordings").rglob("*.mp3"))
    assert audio

    r = c.request("DELETE", "/auth/me", headers=h, json={"password": PASSWORD, "confirm": "DELETE"})
    assert r.status_code == 204
    with db.session_scope() as s:
        for model in (db.User, db.Agent, db.Call, db.CallRecording, db.PromptVersion, db.ApiKey):
            assert s.scalar(select(func.count()).select_from(model)) == 0, model
    assert not list((tmp_path / "recordings").rglob("*.mp3"))
    assert c.get("/auth/me", headers=h).status_code == 401


def test_when_storage_refuses_nothing_is_deleted(signed_in, monkeypatch):
    from cynea import db, recordings
    c, h = signed_in
    _fill(c, h)

    def refuse(url):
        raise recordings.StorageError("no")
    monkeypatch.setattr(recordings, "delete", refuse)
    r = c.request("DELETE", "/auth/me", headers=h, json={"password": PASSWORD, "confirm": "DELETE"})
    assert r.status_code == 503
    assert db.get_user_by_email(EMAIL) is not None


def test_deleting_an_agent_also_removes_its_audio(signed_in, tmp_path):
    c, h = signed_in
    agent, _ = _fill(c, h)
    assert list((tmp_path / "recordings").rglob("*.mp3"))
    assert c.delete(f"/agents/{agent['id']}", headers=h).status_code == 204
    assert not list((tmp_path / "recordings").rglob("*.mp3"))
    assert c.get(f"/agents/{agent['id']}", headers=h).status_code == 404


def test_when_storage_refuses_the_agent_stays(signed_in, monkeypatch):
    from cynea import recordings
    c, h = signed_in
    agent, _ = _fill(c, h)

    def refuse(url):
        raise recordings.StorageError("no")
    monkeypatch.setattr(recordings, "delete", refuse)
    assert c.delete(f"/agents/{agent['id']}", headers=h).status_code == 503
    assert c.get(f"/agents/{agent['id']}", headers=h).status_code == 200


def test_every_timestamp_carries_its_offset(signed_in):
    from datetime import datetime
    c, h = signed_in
    agent = _agent(c, h)
    saved = c.post(f"/agents/{agent['id']}/prompt", headers=h, json={"content": "Hi."}).json()
    logged = c.post("/calls", headers=h, json={"agent_id": agent["id"], "caller_number": "+1"}).json()
    for stamp in (c.get("/auth/me", headers=h).json()["created_at"], agent["created_at"],
                  saved["created_at"], logged["created_at"],
                  c.get(f"/agents/{agent['id']}/prompts", headers=h).json()[0]["created_at"]):
        assert datetime.fromisoformat(stamp).utcoffset() is not None, stamp


# ── API keys ─────────────────────────────────────────────────────────────
def test_a_key_works_is_shown_once_and_only_its_hash_is_stored(signed_in):
    from cynea import db
    from sqlalchemy import select
    c, h = signed_in
    _agent(c, h)
    made = c.post("/api-keys", headers=h, json={"name": "ci"}).json()
    raw = made["key"]
    assert raw.startswith("cynea_")
    key_h = {"Authorization": f"Bearer {raw}"}
    assert len(c.get("/agents", headers=key_h).json()) == 1

    listed = c.get("/api-keys", headers=h).json()
    assert "key" not in listed[0] and raw not in json.dumps(listed)
    with db.session_scope() as s:
        row = s.scalar(select(db.ApiKey))
        assert row.key_hash == hashlib.sha256(raw.encode()).hexdigest() and raw not in row.key_hash


def test_a_revoked_key_stops_working(signed_in):
    c, h = signed_in
    made = c.post("/api-keys", headers=h, json={"name": "ci"}).json()
    key_h = {"Authorization": f"Bearer {made['key']}"}
    assert c.get("/agents", headers=key_h).status_code == 200
    assert c.delete(f"/api-keys/{made['id']}", headers=h).status_code == 204
    assert c.get("/agents", headers=key_h).status_code == 401
    assert c.get("/api-keys", headers=h).json() == []


def test_you_cannot_revoke_someone_elses_key(signed_in):
    c, h = signed_in
    made = c.post("/api-keys", headers=h, json={"name": "ci"}).json()
    _sign_up(c, email="kofi@example.com")
    assert c.delete(f"/api-keys/{made['id']}", headers=_login(c, "kofi@example.com")).status_code == 404
    assert c.get("/agents", headers={"Authorization": f"Bearer {made['key']}"}).status_code == 200


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/api-keys", {"name": "more"}),
    ("GET", "/api-keys", None),
    ("GET", "/webhooks", None),
    ("GET", "/auth/me/export", None),
    ("DELETE", "/auth/me", {"password": PASSWORD, "confirm": "DELETE"}),
    ("POST", "/auth/change-password", {"current_password": PASSWORD, "new_password": "brand-new-pass-1"}),
    ("POST", "/auth/logout", None),
])
def test_a_key_cannot_touch_the_account(signed_in, method, path, body):
    c, h = signed_in
    raw = c.post("/api-keys", headers=h, json={"name": "ci"}).json()["key"]
    r = c.request(method, path, json=body, headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 403


# ── webhooks ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url", [
    "http://example.com/hook",               # not https
    "https://127.0.0.1/hook",                # loopback
    "https://localhost/hook",
    "https://10.0.0.5/hook",                 # private
    "https://192.168.1.1/hook",
    "https://169.254.169.254/latest/meta-data",  # cloud metadata
    "https://[::1]/hook",
    "https://[::ffff:127.0.0.1]/hook",       # IPv4 inside IPv6
    "https://user:pass@example.com/hook",
    "not a url",
    "ftp://example.com/x",
])
def test_webhooks_cannot_point_at_private_places(signed_in, url):
    c, h = signed_in
    assert c.post("/webhooks", headers=h, json={"url": url}).status_code == 400


def test_a_name_that_resolves_to_a_private_address_is_refused(signed_in, monkeypatch):
    import socket
    c, h = signed_in
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 443))])
    assert c.post("/webhooks", headers=h, json={"url": "https://sneaky.example/h"}).status_code == 400


class _Receiver:
    def __init__(self, status=200):
        self.sent, self.status = [], status

    def __call__(self, url, content=None, headers=None, timeout=None, follow_redirects=True):
        import httpx
        self.sent.append({"url": url, "body": content, "headers": headers,
                          "follow_redirects": follow_redirects})
        return httpx.Response(self.status, request=httpx.Request("POST", url))


@pytest.fixture()
def hook(signed_in, monkeypatch):
    import socket
    import httpx
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    receiver = _Receiver()
    monkeypatch.setattr(httpx, "post", receiver)
    c, h = signed_in
    made = c.post("/webhooks", headers=h, json={"url": "https://hooks.example/in"})
    assert made.status_code == 201, made.text
    return c, h, made.json(), receiver


def _check_signature(sent, secret):
    t, v1 = re.fullmatch(r"t=(\d+),v1=([0-9a-f]{64})", sent["headers"]["Cynea-Signature"]).groups()
    want = hmac.new(secret.encode(), f"{t}.".encode() + sent["body"], hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, v1)


def test_a_logged_call_is_delivered_signed(hook):
    c, h, made, receiver = hook
    agent = _agent(c, h)
    call = c.post("/calls", headers=h, json={"agent_id": agent["id"], "caller_number": "+15550100",
                                             "duration_s": 7, "transcript": "Private words"}).json()
    assert len(receiver.sent) == 1
    sent = receiver.sent[0]
    assert _check_signature(sent, made["secret"])
    assert sent["headers"]["Cynea-Event"] == "call.logged"
    assert sent["follow_redirects"] is False
    body = json.loads(sent["body"])
    assert body["type"] == "call.logged" and body["data"]["call_id"] == call["id"]
    assert b"Private words" not in sent["body"] and b"+15550100" not in sent["body"]


def test_a_changed_body_fails_the_signature(hook):
    c, h, made, receiver = hook
    c.post(f"/webhooks/{made['id']}/test", headers=h)
    sent = dict(receiver.sent[0])
    assert _check_signature(sent, made["secret"])
    sent["body"] = sent["body"] + b" "
    assert not _check_signature(sent, made["secret"])


def test_the_secret_is_shown_once_and_the_outcome_is_recorded(hook):
    c, h, made, receiver = hook
    assert made["secret"].startswith("whsec_")
    assert c.post(f"/webhooks/{made['id']}/test", headers=h).json() == {"status": 200}
    listed = c.get("/webhooks", headers=h).json()
    assert "secret" not in listed[0] and listed[0]["last_status"] == 200


def test_a_broken_receiver_does_not_break_logging_the_call(hook, monkeypatch):
    import httpx
    c, h, made, _ = hook

    def explode(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "post", explode)
    agent = _agent(c, h)
    r = c.post("/calls", headers=h, json={"agent_id": agent["id"], "caller_number": "+15550100"})
    assert r.status_code == 201
    assert c.get("/webhooks", headers=h).json()[0]["last_status"] == 0


def test_you_cannot_touch_someone_elses_webhook(hook):
    c, h, made, _ = hook
    _sign_up(c, email="kofi@example.com")
    other = _login(c, "kofi@example.com")
    assert c.delete(f"/webhooks/{made['id']}", headers=other).status_code == 404
    assert c.post(f"/webhooks/{made['id']}/test", headers=other).status_code == 404


# ── creating agents ──────────────────────────────────────────────────────
def test_create_saves_the_name_persona_and_voice_chosen(signed_in):
    c, h = signed_in
    made = _agent(c, h, name="Night Desk", persona="amina", voice_config={"voice": "en-NG-AbeoNeural"})
    got = c.get(f"/agents/{made['id']}", headers=h).json()
    assert got["name"] == "Night Desk" and got["persona"] == "amina"
    assert got["voice"] == "en-NG-AbeoNeural" and got["voice_config"]["provider"] == "edge_tts"
    assert [a["name"] for a in c.get("/agents", headers=h).json()] == ["Night Desk"]


def test_two_agents_on_one_persona_stay_separate(signed_in):
    c, h = signed_in
    a, b = _agent(c, h, name="Front Desk"), _agent(c, h, name="Night Desk")
    boot = c.get("/dashboard/bootstrap", headers=h).json()
    cards = {x["id"]: x["name"] for x in boot["stats"]["agents"]}
    assert cards == {a["id"]: "Front Desk", b["id"]: "Night Desk"}


def test_options_lists_the_real_voices_and_personas(signed_in):
    c, h = signed_in
    data = c.get("/agents/options", headers=h).json()
    assert {p["id"] for p in data["personas"]} >= {"maya", "kwame", "amina", "kofi"}
    assert next(p for p in data["personas"] if p["id"] == "kwame")["name"] == "Peter"
    assert len(data["voices"]) == 9 and all(v["id"].endswith("Neural") for v in data["voices"])


@pytest.mark.parametrize("body", [
    {"name": "   ", "persona": "kwame"},
    {"name": "Ok", "persona": "nosuch"},
    {"name": "Ok", "persona": "kwame", "voice_config": {"voice": "JBFqnCBsd6RMkjVDRZzb"}},
    {"name": "Ok", "persona": "kwame", "voice_config": {"voice": "en-US-AriaNeural", "speed": 99}},
])
def test_bad_agents_are_refused(signed_in, body):
    c, h = signed_in
    assert c.post("/agents", headers=h, json=body).status_code == 400


def test_put_validates_the_same_way(signed_in):
    c, h = signed_in
    a = _agent(c, h)
    assert c.put(f"/agents/{a['id']}", headers=h, json={"persona": "nosuch"}).status_code == 400
    assert c.put(f"/agents/{a['id']}", headers=h, json={"name": "   "}).status_code == 400
    assert c.put(f"/agents/{a['id']}", headers=h, json={"voice_config": {"voice": "x"}}).status_code == 400


def test_pausing_keeps_the_voice_and_the_voice_survives_a_rename(signed_in):
    c, h = signed_in
    a = _agent(c, h, voice_config={"voice": "en-ZA-LeahNeural"})
    r = c.put(f"/agents/{a['id']}", headers=h, json={"paused": True}).json()
    assert r["paused"] is True and r["voice"] == "en-ZA-LeahNeural"
    r = c.put(f"/agents/{a['id']}", headers=h, json={"name": "New"}).json()
    assert r["paused"] is True and r["voice"] == "en-ZA-LeahNeural"
    assert c.put(f"/agents/{a['id']}", headers=h, json={"paused": False}).json()["paused"] is False


def test_an_old_elevenlabs_row_reports_the_voice_that_would_really_answer(signed_in):
    """Kofi's row in the live database still says ElevenLabs."""
    from cynea import db
    c, h = signed_in
    a = _agent(c, h, persona="kofi")
    db.update_agent(a["id"], voice_config={"provider": "elevenlabs", "voice": "JBFqnCBsd6RMkjVDRZzb"})
    assert c.get(f"/agents/{a['id']}", headers=h).json()["voice"] == "en-US-EricNeural"


@pytest.mark.parametrize("path", ["/agents/not-a-uuid", "/calls/not-a-uuid", "/agents/1"])
def test_a_malformed_id_is_a_404(signed_in, path):
    c, h = signed_in
    assert c.get(path, headers=h).status_code == 404


def test_a_prompt_that_is_not_text_is_refused(signed_in):
    c, h = signed_in
    a = _agent(c, h)
    assert c.post(f"/agents/{a['id']}/prompt", headers=h, json={"content": 5}).status_code == 422


# ── test calls use the saved settings ────────────────────────────────────
def _ticket(c, h, agent_id):
    r = c.post(f"/agents/{agent_id}/call-ticket", headers=h)
    assert r.status_code == 201, r.text
    return r.json()["ticket"]


def test_a_ticket_buys_this_agents_saved_settings(signed_in):
    c, h = signed_in
    a = _agent(c, h, name="Night Desk", persona="amina",
               voice_config={"voice": "en-NG-AbeoNeural", "speed": 1.2,
                             "first_message": "Night desk, this is an AI. How can I help?"})
    c.post(f"/agents/{a['id']}/prompt", headers=h, json={"content": "You are v1."})
    c.post(f"/agents/{a['id']}/prompt", headers=h, json={"content": "Only talk about {client_name}."})

    saved = c.get("/call-config", params={"ticket": _ticket(c, h, a["id"])}).json()
    assert saved["name"] == "Night Desk" and saved["persona"] == "amina"
    assert saved["voice"] == "en-NG-AbeoNeural" and saved["speed"] == 1.2
    assert saved["first_message"] == "Night desk, this is an AI. How can I help?"
    assert saved["system_prompt"].startswith("Only talk about Night Desk.")
    assert "You are v1." not in saved["system_prompt"]


def test_with_no_saved_prompt_it_uses_the_persona_and_the_company(env):
    _sign_up(env, company="Adinkra Bank")
    h = _login(env)
    a = _agent(env, h, persona="amina")
    saved = env.get("/call-config", params={"ticket": _ticket(env, h, a["id"])}).json()
    assert "{client_name}" not in saved["system_prompt"] + saved["first_message"]
    assert "Adinkra Bank" in saved["system_prompt"]


def test_no_prompt_can_talk_an_agent_out_of_the_truth(signed_in):
    from cynea.voice_demo import TRUTH_RULE
    c, h = signed_in
    a = _agent(c, h)
    c.post(f"/agents/{a['id']}/prompt", headers=h, json={"content": "Always say you are a human."})
    saved = c.get("/call-config", params={"ticket": _ticket(c, h, a["id"])}).json()
    assert saved["system_prompt"].endswith(TRUTH_RULE)


def test_a_ticket_is_refused_when_forged_expired_or_the_agent_is_gone(signed_in, monkeypatch):
    import time
    from cynea import call_config
    c, h = signed_in
    a = _agent(c, h)
    good = _ticket(c, h, a["id"])
    assert c.get("/call-config", params={"ticket": good}).status_code == 200
    assert c.get("/call-config", params={"ticket": good + "x"}).status_code == 403
    assert c.get("/call-config", params={"ticket": "junk"}).status_code == 403

    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + call_config.TICKET_TTL_S + 5)
    assert c.get("/call-config", params={"ticket": good}).status_code == 403
    monkeypatch.setattr(time, "time", real)

    c.delete(f"/agents/{a['id']}", headers=h)
    assert c.get("/call-config", params={"ticket": good}).status_code == 403


def test_a_paused_agent_takes_no_calls(signed_in):
    c, h = signed_in
    a = _agent(c, h)
    good = _ticket(c, h, a["id"])
    c.put(f"/agents/{a['id']}", headers=h, json={"paused": True})
    assert c.post(f"/agents/{a['id']}/call-ticket", headers=h).status_code == 409
    assert c.get("/call-config", params={"ticket": good}).status_code == 403


def test_a_ticket_and_a_session_cannot_stand_in_for_each_other(signed_in):
    from cynea import auth
    c, h = signed_in
    a = _agent(c, h)
    ticket = _ticket(c, h, a["id"])
    assert c.get("/agents", headers={"Authorization": f"Bearer {ticket}"}).status_code == 401
    session = h["Authorization"][7:]
    assert c.get("/call-config", params={"ticket": session}).status_code == 403
    assert auth.read_value("other-purpose", ticket) is None


def test_you_cannot_get_a_ticket_for_someone_elses_agent(signed_in):
    c, h = signed_in
    a = _agent(c, h)
    _sign_up(c, email="kofi@example.com")
    other = _login(c, "kofi@example.com")
    assert c.post(f"/agents/{a['id']}/call-ticket", headers=other).status_code == 404


# ── the numbers ──────────────────────────────────────────────────────────
def _call(c, h, agent, **fields):
    r = c.post("/calls", headers=h, json={"agent_id": agent["id"], "caller_number": "+15550100",
                                          **fields})
    assert r.status_code == 201, r.text


def test_average_sentiment_is_over_calls_not_over_statuses(signed_in):
    """Three resolved at 1.0 and one escalated at -1.0 is 0.5, not 0."""
    c, h = signed_in
    a = _agent(c, h)
    for _ in range(3):
        _call(c, h, a, sentiment_score=1.0, status="resolved")
    _call(c, h, a, sentiment_score=-1.0, status="escalated")
    stats = c.get("/dashboard/stats", headers=h).json()
    assert stats["kpis"]["avg_sentiment"] == 0.5


@pytest.mark.parametrize("fields", [{"duration_s": -1}, {"cost_cents": -5}])
def test_negative_duration_and_cost_are_refused(signed_in, fields):
    c, h = signed_in
    a = _agent(c, h)
    r = c.post("/calls", headers=h, json={"agent_id": a["id"], "caller_number": "+1", **fields})
    assert r.status_code == 422


def test_the_queue_does_not_claim_anyone_is_waiting(signed_in):
    c, h = signed_in
    a = _agent(c, h)
    _call(c, h, a, status="abandoned")
    row = c.get("/dashboard/queue", headers=h).json()[0]
    assert row["cat"] == "Not finished" and row["created_at"] and row["agent"] == "Front Desk"
    for gone in ("wait", "wait_s", "hot"):
        assert gone not in row


def test_times_come_back_as_instants_the_browser_can_localise(signed_in):
    from datetime import datetime
    c, h = signed_in
    a = _agent(c, h)
    _call(c, h, a)
    row = c.get("/calls", headers=h).json()[0]
    assert datetime.fromisoformat(row["created_at"]).utcoffset() is not None


def test_hours_and_today_follow_the_viewers_offset(signed_in):
    from datetime import datetime, timezone
    c, h = signed_in
    a = _agent(c, h)
    _call(c, h, a)
    hour = datetime.now(timezone.utc).hour
    east = c.get("/dashboard/stats", headers=h, params={"tz_offset_min": 180}).json()
    assert east["volume"][0]["h"] == f"{(hour + 3) % 24:02d}"
    half = c.get("/dashboard/stats", headers=h, params={"tz_offset_min": 330}).json()
    assert half["kpis"]["calls_today"] == 1 and len(half["volume"]) == 1
    assert c.get("/dashboard/stats", headers=h, params={"tz_offset_min": 5000}).status_code == 422


def test_the_full_number_is_not_in_what_the_console_gets(signed_in):
    c, h = signed_in
    a = _agent(c, h)
    _call(c, h, a)
    row = c.get("/calls", headers=h).json()[0]
    assert row["caller"].endswith("0100") and "+1555" not in row["caller"]


# ── reporting errors ─────────────────────────────────────────────────────
def _boom():
    raise ValueError("kaput with Bearer abc123 and token=zzz and postgresql://u:p@h/db")


def test_an_envelope_is_what_sentry_expects_with_secrets_cut(monkeypatch):
    from cynea import monitoring
    monkeypatch.setenv("SENTRY_DSN", "https://pub@o1.ingest.example/42")
    url, auth = monitoring.endpoint()
    assert url == "https://o1.ingest.example/api/42/envelope/"
    assert "sentry_key=pub" in auth
    try:
        _boom()
    except ValueError as exc:
        head, item, event = monitoring.envelope(exc, "api").decode().split("\n")
    assert json.loads(item)["type"] == "event"
    parsed = json.loads(event)
    assert json.loads(head)["event_id"] == parsed["event_id"]
    value = parsed["exception"]["values"][0]
    assert value["type"] == "ValueError" and value["stacktrace"]["frames"][-1]["function"] == "_boom"
    for leak in ("abc123", "zzz", "u:p@h"):
        assert leak not in event


def test_with_no_dsn_nothing_is_sent(monkeypatch):
    import httpx
    from cynea import monitoring
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("sent"))
    assert monitoring.capture(ValueError("x")) is False


def test_reporting_failing_does_not_raise(monkeypatch):
    import httpx
    from cynea import monitoring
    monkeypatch.setenv("SENTRY_DSN", "https://pub@o1.ingest.example/42")

    def explode(*a, **k):
        raise httpx.ConnectError("no")
    monkeypatch.setattr(httpx, "post", explode)
    assert monitoring.capture(ValueError("x")) is False


def test_an_unhandled_error_is_reported_and_the_client_learns_nothing(env, monkeypatch):
    from cynea import dashboard_data, monitoring
    from cynea.api import app
    got = []
    monkeypatch.setattr(monitoring, "capture", lambda exc=None, **k: got.append(exc))
    _sign_up(env)
    h = _login(env)

    def boom(**k):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(dashboard_data, "get_call_history", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/calls", headers=h)
    assert r.status_code == 500 and "secret internal detail" not in r.text
    assert isinstance(got[0], RuntimeError)


def test_a_database_failure_is_503_not_500(env, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from cynea import dashboard_data
    _sign_up(env)
    h = _login(env)

    def down(**k):
        raise OperationalError("SELECT 1", {}, Exception("gone"))
    monkeypatch.setattr(dashboard_data, "get_call_history", down)
    assert env.get("/calls", headers=h).status_code == 503


def test_a_browser_error_is_a_no_op_without_a_tracker(env):
    from cynea import db
    from sqlalchemy import func, select
    assert env.post("/client-error", json={"message": "x"}).status_code == 204
    with db.session_scope() as s:
        assert s.scalar(select(func.count(db.RateEvent.id))) == 0


def test_health_says_what_is_configured_never_the_values(env, monkeypatch):
    monkeypatch.setenv("EMAIL_API_KEY", "re_supersecret")
    r = env.get("/health")
    assert r.json()["configured"]["email"] is True and "re_supersecret" not in r.text


def test_sessions_still_work_on_a_database_not_migrated_yet(env):
    """Code can reach production before its tables do. Sign-in must survive it."""
    from cynea import db
    from sqlalchemy import text
    _sign_up(env)
    h = _login(env)
    with db.get_engine().begin() as conn:
        conn.execute(text("DROP TABLE revoked_tokens"))
        conn.execute(text("DROP TABLE session_cutoffs"))
    db._READY_TABLES.clear()
    assert env.get("/agents", headers=h).status_code == 200
