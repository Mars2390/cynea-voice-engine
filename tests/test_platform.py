"""Sign-up + email verification, recording storage, and the production docs lock.

SQLite through FastAPI's TestClient, no network: the mailer is captured,
Turnstile's verify call is faked, and recordings go to a temp directory.
"""

import importlib
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p.db'}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("EMAIL_DEV_LOG_LINKS", "1")
    for k in ("EMAIL_API_KEY", "TURNSTILE_SECRET_KEY", "TURNSTILE_SITE_KEY", "ENV",
              "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(k, raising=False)
    from cynea import mailer
    sent = {}
    monkeypatch.setattr(mailer, "send_verification",
                        lambda to, link, name="": sent.__setitem__(to, link) or "logged")
    from cynea import db
    db.reset_connection()
    db.init_db()
    from cynea.api import app
    with TestClient(app) as c:
        c.sent = sent
        yield c
    db.reset_connection()


def _register(c, email="ama@example.com", **extra):
    return c.post("/auth/register", json={"email": email, "password": "demo1234", **extra})


def _path(link):
    return link[link.index("/auth/verify"):]


def _approve(email="ama@example.com"):
    """What an administrator does on admin.html."""
    from cynea import db
    db.set_user_status(db.get_user_by_email(email).id, db.APPROVED)


def _signed_in(c, email="ama@example.com"):
    assert _register(c, email).status_code == 201
    c.get(_path(c.sent[email]), follow_redirects=False)
    _approve(email)
    r = c.post("/auth/login", json={"email": email, "password": "demo1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


# ── sign-up ──────────────────────────────────────────────────────────────
def test_duplicate_email_says_only_that(client):
    _register(client)
    r = _register(client)
    assert r.status_code == 400
    assert r.json()["detail"] == "Email already registered"
    assert "ama@" not in r.text


def test_name_and_company_are_stored(client):
    _register(client, name="Ama Mensah", company="Adinkra Hotel")
    from cynea import db
    u = db.get_user_by_email("ama@example.com")
    assert (u.full_name, u.company) == ("Ama Mensah", "Adinkra Hotel")


def test_verify_link_redirects_to_signin_and_approval_unlocks_login(client):
    _register(client)
    r = client.get(_path(client.sent["ama@example.com"]), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/signin.html?verified=1")
    login = {"email": "ama@example.com", "password": "demo1234"}
    assert client.post("/auth/login", json=login).json()["detail"] == \
        "Your account is waiting for approval."
    _approve()
    assert client.post("/auth/login", json=login).status_code == 200


def test_token_is_stored_hashed_and_single_use(client):
    _register(client)
    link = client.sent["ama@example.com"]
    raw = link.split("token=")[1]
    from cynea import db
    u = db.get_user_by_email("ama@example.com")
    assert u.verify_token_hash and raw not in u.verify_token_hash
    client.get(_path(link), follow_redirects=False)
    again = client.get(_path(link), follow_redirects=False)
    assert again.headers["location"].endswith("signin.html?verify=invalid")


def test_expired_link_is_refused(client):
    _register(client)
    from cynea import db
    with db.session_scope() as s:
        u = s.query(db.User).filter_by(email="ama@example.com").one()
        u.verify_token_expires = db._now() - timedelta(minutes=1)
    r = client.get(_path(client.sent["ama@example.com"]), follow_redirects=False)
    assert r.headers["location"].endswith("signin.html?verify=invalid")
    assert client.post("/auth/login", json={"email": "ama@example.com",
                                            "password": "demo1234"}).status_code == 403


def test_registration_is_limited_to_five_per_hour_per_ip(client):
    for i in range(5):
        assert _register(client, f"u{i}@example.com").status_code == 201
    r = _register(client, "u5@example.com")
    assert r.status_code == 429
    assert "Try again in an hour" in r.json()["detail"]
    from cynea import db
    assert db.get_user_by_email("u5@example.com") is None


def test_turnstile_is_enforced_when_configured(client, monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "site-key")
    assert client.get("/auth/config").json()["turnstile_site_key"] == "site-key"
    assert _register(client).json()["detail"] == "Bot check failed. Please try again."

    seen = {}

    class R:
        def __init__(self, ok): self.ok = ok
        def json(self): return {"success": self.ok}

    import httpx
    monkeypatch.setattr(httpx, "post", lambda url, data=None, timeout=None:
                        seen.update(data) or R(data["response"] == "good"))
    assert _register(client, turnstile_token="bad").status_code == 400
    assert _register(client, turnstile_token="good").status_code == 201
    assert seen["secret"] == "secret"


def test_turnstile_is_skipped_without_keys(client):
    assert client.get("/auth/config").json()["turnstile_site_key"] is None
    assert _register(client).status_code == 201


def test_resend_never_reveals_accounts_and_replaces_the_link(client):
    unknown = client.post("/auth/resend-verification", json={"email": "nobody@example.com"})
    _register(client)
    first = client.sent["ama@example.com"]
    known = client.post("/auth/resend-verification", json={"email": "ama@example.com"})
    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    assert client.sent["ama@example.com"] != first
    stale = client.get(_path(first), follow_redirects=False)
    assert stale.headers["location"].endswith("verify=invalid")


def test_accounts_from_before_verification_stay_signed_in(tmp_path, monkeypatch):
    """Adding the column must not lock out existing accounts."""
    import sqlalchemy as sa
    url = f"sqlite:///{tmp_path / 'old.db'}"
    eng = sa.create_engine(url)
    with eng.begin() as conn:
        conn.execute(sa.text("CREATE TABLE users (id CHAR(36) PRIMARY KEY, email VARCHAR(320) "
                             "UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL, "
                             "created_at DATETIME NOT NULL)"))
        conn.execute(sa.text("INSERT INTO users VALUES ('u1', 'old@example.com', 'x', "
                             "'2026-08-25 00:00:00')"))
    eng.dispose()
    monkeypatch.setenv("DATABASE_URL", url)
    from cynea import auth, db
    db.reset_connection()
    db.init_db()
    assert auth.is_verified(db.get_user_by_email("old@example.com"))
    assert "users.email_verified_at" not in db.upgrade_schema()   # idempotent
    db.reset_connection()


# ── recordings ───────────────────────────────────────────────────────────
def _call(c, headers):
    agent = c.post("/agents", headers=headers, json={"name": "Desk", "persona": "kwame"}).json()["id"]
    r = c.post("/calls", headers=headers, json={"agent_id": agent, "caller_number": "+15550100",
                                                 "duration_s": 42, "transcript": "Booked."})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload(c, headers, call_id, data=b"ID3fake-mp3-bytes", ctype="audio/mpeg"):
    return c.post(f"/calls/{call_id}/recording", headers=headers,
                  files={"file": ("call.mp3", data, ctype)}, data={"duration_s": "42"})


def test_no_recording_says_so(client):
    h = _signed_in(client)
    r = client.get(f"/calls/{_call(client, h)}/recording", headers=h)
    assert r.status_code == 404 and r.json()["detail"] == "No recording available"


def test_upload_then_play_back_the_same_bytes(client):
    h = _signed_in(client)
    cid = _call(client, h)
    up = _upload(client, h, cid)
    assert up.status_code == 201 and up.json()["storage"] == "local"
    meta = client.get(f"/calls/{cid}/recording", headers=h).json()
    assert meta["duration_s"] == 42 and meta["content_type"] == "audio/mpeg"
    url = meta["url"]
    played = client.get(url[url.index("/calls/"):])          # no bearer: the link is signed
    assert played.status_code == 200 and played.content == b"ID3fake-mp3-bytes"


def test_tampered_or_expired_playback_links_are_refused(client):
    h = _signed_in(client)
    cid = _call(client, h)
    _upload(client, h, cid)
    url = client.get(f"/calls/{cid}/recording", headers=h).json()["url"]
    path = url[url.index("/calls/"):]
    assert client.get(path[:-4] + "0000").status_code == 403
    import re
    expired = re.sub(r"exp=\d+", "exp=1", path)
    assert client.get(expired).status_code == 403


def test_another_workspace_cannot_upload_or_play(client):
    owner = _signed_in(client, "owner@example.com")
    cid = _call(client, owner)
    _upload(client, owner, cid)
    other = _signed_in(client, "other@example.com")
    assert client.get(f"/calls/{cid}/recording", headers=other).status_code == 404
    assert _upload(client, other, cid).status_code == 404


def test_unsupported_type_and_empty_upload_are_rejected(client):
    h = _signed_in(client)
    cid = _call(client, h)
    assert _upload(client, h, cid, ctype="text/plain").status_code == 415
    assert _upload(client, h, cid, data=b"").status_code == 400


def test_a_second_upload_replaces_and_deletes_the_first(client, tmp_path):
    h = _signed_in(client)
    cid = _call(client, h)
    _upload(client, h, cid, data=b"first")
    _upload(client, h, cid, data=b"second")
    files = [p for p in (tmp_path / "recordings").rglob("*") if p.is_file()]
    assert [p.read_bytes() for p in files] == [b"second"]
    from cynea import db
    with db.session_scope() as s:
        assert s.query(db.CallRecording).filter_by(call_id=cid).count() == 1


def test_purge_removes_only_what_is_past_retention(client, tmp_path):
    h = _signed_in(client)
    old_call, new_call = _call(client, h), _call(client, h)
    _upload(client, h, old_call, data=b"old")
    _upload(client, h, new_call, data=b"new")
    from cynea import db
    with db.session_scope() as s:
        rec = s.query(db.CallRecording).filter_by(call_id=old_call).one()
        rec.created_at = db._now() - timedelta(days=31)
    purge = importlib.import_module("tools.purge_recordings")
    assert purge.purge(days=30, dry_run=True)["deleted"] == 0
    r = purge.purge(days=30)
    assert (r["expired"], r["deleted"], r["failed"]) == (1, 1, 0)
    assert client.get(f"/calls/{old_call}/recording", headers=h).status_code == 404
    assert client.get(f"/calls/{new_call}/recording", headers=h).status_code == 200
    left = [p.read_bytes() for p in (tmp_path / "recordings").rglob("*") if p.is_file()]
    assert left == [b"new"]


def test_r2_is_chosen_when_configured_and_links_are_presigned(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    for k, v in {"R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "AKID",
                 "R2_SECRET_ACCESS_KEY": "secret", "R2_BUCKET": "calls"}.items():
        monkeypatch.setenv(k, v)
    from cynea import recordings
    assert recordings.backend() == "r2"
    url = recordings.playback_url("r2://calls/calls/c1/x.mp3", "c1", "https://x/api/")
    assert url.startswith("https://acct.r2.cloudflarestorage.com/calls/calls/c1/x.mp3?")
    assert "X-Amz-Signature=" in url and "X-Amz-Expires=600" in url

    put = {}

    class Fake:
        def put_object(self, **kw): put.update(kw)

    monkeypatch.setattr(recordings, "_r2_client", lambda cfg: Fake())
    key, audio_url = recordings.store("c1", b"abc", "audio/wav")
    assert audio_url == f"r2://calls/{key}" and put["Body"] == b"abc" and key.endswith(".wav")


def test_half_configured_r2_is_an_error_not_a_silent_fallback(monkeypatch):
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "AKID")
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("R2_BUCKET", raising=False)
    from cynea import recordings
    with pytest.raises(recordings.StorageError):
        recordings.backend()


# ── production docs lock ─────────────────────────────────────────────────
def test_docs_are_off_and_schema_needs_a_session_in_production(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'prod.db'}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("ENV", "production")
    from cynea import api, auth, db
    db.reset_connection()
    db.init_db()
    prod = importlib.reload(api)
    try:
        with TestClient(prod.app) as c:
            assert c.get("/docs").status_code == 404
            assert c.get("/redoc").status_code == 404
            assert c.get("/openapi.json").status_code == 401
            # A real account: a session for nobody is now refused on every route.
            token = auth.create_session(db.create_user("a@example.com", "x").id)
            r = c.get("/openapi.json", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 200 and "/auth/login" in r.json()["paths"]
    finally:
        monkeypatch.delenv("ENV")
        importlib.reload(api)
        db.reset_connection()
