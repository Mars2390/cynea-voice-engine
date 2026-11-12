"""Account approval and the admin section, through the API, on SQLite.

Sign-up with no email at all, what each status may do at sign-in, the three
/admin routes and who can reach them, and the migration that adds the columns.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

PASSWORD = "correct-horse-1"
ADMIN, AMA = "admin@example.com", "ama@example.com"
WAITING = "Your account is waiting for approval."
TURNED_DOWN = "Your account was not approved."


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """No email configured at all: the situation this feature exists for."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path / "recordings"))
    # Empty rather than deleted: importing cynea loads .env without overriding,
    # so a deleted variable comes back from a developer's .env (ENV=production,
    # a real EMAIL_API_KEY) and an empty one does not.
    for k in ("EMAIL_API_KEY", "EMAIL_DEV_LOG_LINKS", "TURNSTILE_SECRET_KEY",
              "TURNSTILE_SITE_KEY", "ENV", "VERCEL", "SITE_URL", "PUBLIC_API_URL",
              "VERCEL_PROJECT_PRODUCTION_URL", "SENTRY_DSN"):
        monkeypatch.setenv(k, "")
    from cynea import db
    db.reset_connection()
    db.init_db()
    from cynea.api import app
    with TestClient(app) as c:
        yield c
    db.reset_connection()


def _register(c, email, **extra):
    r = c.post("/auth/register", json={"email": email, "password": PASSWORD, **extra})
    assert r.status_code == 201, r.text
    return r


def _login(c, email):
    return c.post("/auth/login", json={"email": email, "password": PASSWORD})


def _bearer(r):
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _id(email):
    from cynea import db
    return db.get_user_by_email(email).id


@pytest.fixture()
def admin(env):
    """An admin made the way production makes one: the migration's flag."""
    from cynea import db
    _register(env, ADMIN)
    assert db.make_admin(ADMIN) is not None
    return env, _bearer(_login(env, ADMIN))


# ── sign-up with no email ─────────────────────────────────────────────────
def test_sign_up_works_with_no_email_key_and_logs_the_link(env, caplog):
    from cynea import db
    with caplog.at_level(logging.WARNING, logger="cynea.api"):
        r = _register(env, AMA, name="Ama Mensah", company="Adinkra Hotel")
    assert r.json() == {"status": "pending_approval", "email": AMA,
                        "message": "Account created. An administrator will review it shortly."}
    user = db.get_user_by_email(AMA)
    assert db.user_status(user) == db.PENDING and user.is_admin is False
    logged = [m for m in caplog.messages if "/auth/verify?token=" in m]
    assert len(logged) == 1 and AMA in logged[0]
    # The link in the log is a working one: following it verifies the address.
    path = logged[0][logged[0].index("/auth/verify"):]
    env.get(path, follow_redirects=False)
    assert db.get_user_by_email(AMA).email_verified_at is not None


def test_a_failed_send_does_not_fail_the_sign_up(env, monkeypatch):
    from cynea import mailer

    def down(*a, **k):
        raise mailer.EmailError("provider down")
    monkeypatch.setenv("EMAIL_API_KEY", "re_test")
    monkeypatch.setattr(mailer, "send_verification", down)
    assert _register(env, AMA).json()["status"] == "pending_approval"


# ── sign-in by status ─────────────────────────────────────────────────────
def test_pending_account_cannot_sign_in(env):
    _register(env, AMA)
    r = _login(env, AMA)
    assert r.status_code == 403
    assert r.json() == {"error": "AwaitingApproval", "detail": WAITING}


def test_a_wrong_password_on_a_pending_account_is_still_just_wrong(env):
    """Status is only told to someone who knows the password."""
    _register(env, AMA)
    r = env.post("/auth/login", json={"email": AMA, "password": "not-the-password"})
    assert r.status_code == 401 and WAITING not in r.text


def test_when_email_works_an_unverified_pending_account_is_asked_to_verify(env, monkeypatch):
    from cynea import mailer
    links = {}
    monkeypatch.setenv("EMAIL_API_KEY", "re_test")
    monkeypatch.setattr(mailer, "send_verification",
                        lambda to, link, name="": links.__setitem__(to, link) or "sent")
    _register(env, AMA)
    assert _login(env, AMA).json()["detail"] == "Please verify your email first."
    env.get(links[AMA][links[AMA].index("/auth/verify"):], follow_redirects=False)
    assert _login(env, AMA).json()["detail"] == WAITING


def test_approved_account_signs_in(admin):
    c, h = admin
    _register(c, AMA)
    r = c.post(f"/admin/users/{_id(AMA)}/approve", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    me = c.get("/auth/me", headers=_bearer(_login(c, AMA))).json()
    assert me["status"] == "approved" and me["is_admin"] is False


def test_approval_records_when_and_by_whom(admin):
    from cynea import db
    c, h = admin
    _register(c, AMA)
    c.post(f"/admin/users/{_id(AMA)}/approve", headers=h)
    user = db.get_user_by_email(AMA)
    assert user.status == db.APPROVED
    assert user.approved_at is not None and user.approved_by == _id(ADMIN)
    first = user.approved_at
    c.post(f"/admin/users/{_id(AMA)}/approve", headers=h)          # a second click
    assert db.get_user_by_email(AMA).approved_at == first


def test_approval_stands_in_for_verification_while_no_email_can_be_sent(admin):
    from cynea import auth, db
    c, h = admin
    _register(c, AMA)
    c.post(f"/admin/users/{_id(AMA)}/approve", headers=h)
    assert not auth.is_verified(db.get_user_by_email(AMA))
    assert _login(c, AMA).status_code == 200


def test_rejected_account_cannot_sign_in(admin):
    from cynea import db
    c, h = admin
    _register(c, AMA)
    r = c.post(f"/admin/users/{_id(AMA)}/reject", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert db.get_user_by_email(AMA).approved_at is None
    r = _login(c, AMA)
    assert r.status_code == 403
    assert r.json() == {"error": "NotApproved", "detail": TURNED_DOWN}


def test_rejecting_ends_sessions_and_stops_api_keys(admin):
    c, h = admin
    _register(c, AMA)
    c.post(f"/admin/users/{_id(AMA)}/approve", headers=h)
    ama = _bearer(_login(c, AMA))
    key = c.post("/api-keys", headers=ama, json={"name": "server"}).json()["key"]
    keyed = {"Authorization": f"Bearer {key}"}
    assert c.get("/agents", headers=keyed).status_code == 200

    c.post(f"/admin/users/{_id(AMA)}/reject", headers=h)
    assert c.get("/auth/me", headers=ama).status_code == 401
    assert c.get("/agents", headers=keyed).status_code == 401

    # Approved again: the key works again, the old session stays dead.
    c.post(f"/admin/users/{_id(AMA)}/approve", headers=h)
    assert c.get("/agents", headers=keyed).status_code == 200
    assert c.get("/auth/me", headers=ama).status_code == 401
    assert _login(c, AMA).status_code == 200


# ── who can reach /admin ──────────────────────────────────────────────────
def _admin_routes(target_id):
    return [("GET", "/admin/users"),
            ("POST", f"/admin/users/{target_id}/approve"),
            ("POST", f"/admin/users/{target_id}/reject")]


def test_admin_routes_need_a_session(admin):
    c, _ = admin
    _register(c, AMA)
    for method, path in _admin_routes(_id(AMA)):
        assert c.request(method, path).status_code == 401, path
        assert c.request(method, path, headers={"Authorization": "Bearer forged.token"}
                         ).status_code == 401, path


def test_a_signed_in_non_admin_gets_403_and_changes_nothing(admin):
    from cynea import db
    c, h = admin
    _register(c, "kofi@example.com")
    c.post(f"/admin/users/{_id('kofi@example.com')}/approve", headers=h)
    kofi = _bearer(_login(c, "kofi@example.com"))
    _register(c, AMA)
    for method, path in _admin_routes(_id(AMA)):
        r = c.request(method, path, headers=kofi)
        assert r.status_code == 403, path
        assert r.json()["detail"] == "This needs an administrator account."
    assert db.user_status(db.get_user_by_email(AMA)) == db.PENDING


def test_losing_the_flag_takes_effect_on_the_next_request(admin):
    """The check reads the database every time; a token minted as an admin
    carries no admin claim of its own."""
    from cynea import db
    c, h = admin
    assert c.get("/admin/users", headers=h).status_code == 200
    with db.session_scope() as s:
        s.get(db.User, _id(ADMIN)).is_admin = False
    assert c.get("/admin/users", headers=h).status_code == 403


def test_an_admins_api_key_cannot_use_admin_routes(admin):
    c, h = admin
    key = c.post("/api-keys", headers=h, json={"name": "ci"}).json()["key"]
    r = c.get("/admin/users", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403


def test_an_admin_cannot_reject_themselves(admin):
    c, h = admin
    r = c.post(f"/admin/users/{_id(ADMIN)}/reject", headers=h)
    assert r.status_code == 400
    assert c.get("/admin/users", headers=h).status_code == 200


def test_unknown_or_malformed_ids_are_404(admin):
    c, h = admin
    for target in ("00000000-0000-0000-0000-000000000000", "not-a-uuid"):
        assert c.post(f"/admin/users/{target}/approve", headers=h).status_code == 404
        assert c.post(f"/admin/users/{target}/reject", headers=h).status_code == 404


# ── the list ──────────────────────────────────────────────────────────────
def test_list_shows_every_account_with_the_fields_the_table_needs(admin):
    c, h = admin
    _register(c, AMA, name="Ama Mensah", company="Adinkra Hotel")
    body = c.get("/admin/users", headers=h).json()
    assert body["total"] == 2
    ama = next(u for u in body["users"] if u["email"] == AMA)
    assert (ama["name"], ama["company"], ama["status"]) == ("Ama Mensah", "Adinkra Hotel", "pending")
    assert ama["created_at"] and ama["email_verified"] is False
    assert "password_hash" not in ama and "verify_token_hash" not in ama
    assert body["users"][0]["email"] == AMA                      # newest first


def test_list_filters_by_status_but_counts_everything(admin):
    c, h = admin
    for email in ("a@example.com", "b@example.com", "r@example.com"):
        _register(c, email)
    c.post(f"/admin/users/{_id('b@example.com')}/approve", headers=h)
    c.post(f"/admin/users/{_id('r@example.com')}/reject", headers=h)

    def emails(status):
        return {u["email"] for u in c.get(f"/admin/users?status={status}", headers=h).json()["users"]}

    assert emails("pending") == {"a@example.com"}
    assert emails("approved") == {"b@example.com", ADMIN}
    assert emails("rejected") == {"r@example.com"}
    body = c.get("/admin/users?status=pending", headers=h).json()
    assert body["counts"] == {"pending": 1, "approved": 2, "rejected": 1}
    assert c.get("/admin/users?status=nonsense", headers=h).status_code == 400


def test_me_says_whether_the_page_should_show_the_admin_link(admin):
    c, h = admin
    assert c.get("/auth/me", headers=h).json()["is_admin"] is True


# ── the migration ─────────────────────────────────────────────────────────
def _old_database(tmp_path):
    """A users table as production had it before approval existed."""
    import sqlalchemy as sa
    url = f"sqlite:///{tmp_path / 'old.db'}"
    eng = sa.create_engine(url)
    with eng.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE users (id CHAR(36) PRIMARY KEY, email VARCHAR(320) UNIQUE NOT NULL, "
            "password_hash VARCHAR(255) NOT NULL, created_at DATETIME NOT NULL, "
            "full_name VARCHAR(120), company VARCHAR(160), email_verified_at DATETIME, "
            "verify_token_hash VARCHAR(64), verify_token_expires DATETIME)"))
        conn.execute(sa.text(
            "INSERT INTO users (id, email, password_hash, created_at, email_verified_at) VALUES "
            "('11111111-1111-1111-1111-111111111111', 'owner@example.com', 'x', "
            " '2026-10-01 00:00:00', '2026-10-01 00:05:00'), "
            "('22222222-2222-2222-2222-222222222222', 'never@example.com', 'x', "
            " '2026-10-02 00:00:00', NULL)"))
    eng.dispose()
    return url


def test_migration_keeps_verified_accounts_working_and_holds_unverified_ones(tmp_path, monkeypatch):
    from cynea import db
    monkeypatch.setenv("DATABASE_URL", _old_database(tmp_path))
    db.reset_connection()
    added = db.upgrade_schema()
    assert {"users.status", "users.approved_at", "users.approved_by", "users.is_admin"} <= set(added)
    assert db.get_user_by_email("owner@example.com").status == db.APPROVED
    assert db.get_user_by_email("never@example.com").status == db.PENDING
    assert db.get_user_by_email("owner@example.com").is_admin is False
    assert db.upgrade_schema() == []                                  # idempotent
    db.reset_connection()


def test_migrate_check_changes_nothing_and_make_admin_sets_the_flag(tmp_path, monkeypatch, capsys):
    from cynea import db, migrate
    import sqlalchemy as sa
    url = _old_database(tmp_path)
    monkeypatch.setenv("DATABASE_URL", url)
    db.reset_connection()

    assert migrate.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "users.status" in out and "users.is_admin" in out
    assert "no approval columns yet" in out
    cols = {c["name"] for c in sa.inspect(sa.create_engine(url)).get_columns("users")}
    assert "status" not in cols                                       # --check wrote nothing

    assert migrate.main(["--make-admin", "OWNER@example.com"]) == 0
    out = capsys.readouterr().out
    assert "Admin: owner@example.com" in out
    assert "approved 1" in out and "pending 1" in out and "admins 1" in out
    assert db.get_user_by_email("owner@example.com").is_admin is True

    assert migrate.main(["--make-admin", "nobody@example.com"]) == 1
    assert migrate.main(["--check", "--make-admin", "owner@example.com"]) == 2
    db.reset_connection()


def test_no_address_is_special_in_the_code():
    """Admin is a database flag. No route or module may name an account."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent / "cynea"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "admin" in line.lower():
                assert not re.search(r"[\w.+-]+@(gmail|cynea)\.\w+", line), f"{path.name}: {line}"
