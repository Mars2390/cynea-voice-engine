"""Tests for cynea.auth — password hashing and session tokens.

The properties worth pinning are the security ones: hashes are salted,
login does not reveal whether an email exists, and a forged or expired
token is rejected. Those are the failures that matter, and they are silent
if you only test the happy path.
"""

import time

import pytest

from cynea import auth


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from cynea import db as dbmod
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'auth.db'}")
    dbmod.reset_connection()
    dbmod.init_db()
    yield dbmod
    dbmod.reset_connection()


@pytest.fixture()
def secret(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret-do-not-use-in-production")


# ----------------------------------------------------------------------
# Password hashing
# ----------------------------------------------------------------------

def test_hash_then_verify_round_trips():
    h = auth.hash_password("correct horse battery")
    assert auth.verify_password("correct horse battery", h)


def test_wrong_password_fails():
    h = auth.hash_password("correct horse battery")
    assert not auth.verify_password("wrong horse battery", h)


def test_hash_is_salted():
    """Two users with the same password must not share a hash."""
    a = auth.hash_password("same-password")
    b = auth.hash_password("same-password")
    assert a != b
    assert auth.verify_password("same-password", a)
    assert auth.verify_password("same-password", b)


def test_plaintext_never_appears_in_the_hash():
    h = auth.hash_password("hunter2hunter2")
    assert "hunter2" not in h


@pytest.mark.parametrize("bad", ["", "short", "1234567"])
def test_short_passwords_are_rejected(bad):
    with pytest.raises(auth.WeakPassword):
        auth.hash_password(bad)


def test_over_72_bytes_is_rejected():
    """bcrypt silently truncates past 72 bytes; a user must not believe a
    long passphrase is protecting them when only the first 72 bytes are."""
    with pytest.raises(auth.WeakPassword):
        auth.hash_password("a" * 73)


def test_verify_returns_false_on_a_malformed_hash():
    assert not auth.verify_password("anything", "not-a-bcrypt-hash")


# ----------------------------------------------------------------------
# Registration and login
# ----------------------------------------------------------------------

def test_register_then_login(db):
    auth.register_user("ama@example.com", "demo1234")
    user = auth.login_user("ama@example.com", "demo1234")
    assert user.email == "ama@example.com"


def test_registration_is_case_insensitive(db):
    auth.register_user("Ama@Example.COM", "demo1234")
    assert auth.login_user("ama@example.com", "demo1234")


def test_duplicate_registration_rejected(db):
    auth.register_user("ama@example.com", "demo1234")
    with pytest.raises(auth.EmailAlreadyRegistered):
        auth.register_user("ama@example.com", "another1234")


def test_invalid_email_rejected(db):
    with pytest.raises(auth.AuthError):
        auth.register_user("not-an-email", "demo1234")


def test_wrong_password_raises_invalid_credentials(db):
    auth.register_user("ama@example.com", "demo1234")
    with pytest.raises(auth.InvalidCredentials):
        auth.login_user("ama@example.com", "wrong-password")


def test_unknown_email_gives_the_same_error_as_a_wrong_password(db):
    """Otherwise the endpoint enumerates which emails are registered."""
    auth.register_user("ama@example.com", "demo1234")
    with pytest.raises(auth.InvalidCredentials) as unknown:
        auth.login_user("nobody@example.com", "demo1234")
    with pytest.raises(auth.InvalidCredentials) as wrong:
        auth.login_user("ama@example.com", "nope1234")
    assert str(unknown.value) == str(wrong.value)


def test_password_is_not_stored_in_plaintext(db):
    auth.register_user("ama@example.com", "demo1234")
    stored = db.get_user_by_email("ama@example.com").password_hash
    assert "demo1234" not in stored
    assert stored.startswith("$2")   # bcrypt marker


# ----------------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------------

def test_session_round_trips(secret):
    token = auth.create_session("user-123")
    assert auth.validate_session(token) == "user-123"


def test_tampered_payload_is_rejected(secret):
    token = auth.create_session("user-123")
    body, _, sig = token.partition(".")
    forged = auth.create_session("attacker")
    other_body = forged.partition(".")[0]
    with pytest.raises(auth.InvalidSession):
        auth.validate_session(other_body + "." + sig)


def test_expired_token_is_rejected(secret):
    token = auth.create_session("user-123", ttl_seconds=-1)
    with pytest.raises(auth.InvalidSession):
        auth.validate_session(token)


@pytest.mark.parametrize("bad", ["", "garbage", "no-dot-here", "a.b", "..", "x."])
def test_malformed_tokens_are_rejected(secret, bad):
    with pytest.raises(auth.InvalidSession):
        auth.validate_session(bad)


def test_token_from_a_different_secret_is_rejected(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "secret-one")
    token = auth.create_session("user-123")
    monkeypatch.setenv("SESSION_SECRET", "secret-two")
    with pytest.raises(auth.InvalidSession):
        auth.validate_session(token)


def test_two_sessions_for_one_user_differ(secret):
    """The jti keeps otherwise-identical tokens distinct."""
    assert auth.create_session("u") != auth.create_session("u")


def test_current_user_rejects_a_token_for_a_deleted_account(db, secret):
    user = auth.register_user("ama@example.com", "demo1234")
    token = auth.create_session(user.id)
    assert auth.current_user(token).email == "ama@example.com"

    with db.session_scope() as s:
        s.delete(s.get(db.User, user.id))

    with pytest.raises(auth.InvalidSession):
        auth.current_user(token)
