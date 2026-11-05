"""Cynea Voice Engine — authentication.

Password hashing with bcrypt, plus signed stateless session tokens.

What this is
------------
Enough auth to put a real login in front of the console, replacing
`signin.html`'s redirect-and-hope. Passwords are bcrypt-hashed with a
per-password salt, and sessions are HMAC-signed tokens carrying an expiry.

What this is not
----------------
Not an identity provider: no MFA and no OAuth. Email verification, password
reset, sign-out and "every session dies when the password changes" are here;
past that, move to a hosted provider rather than growing this file.

Token format
------------
    base64url(payload_json) + "." + base64url(hmac_sha256(payload))

validate_session() checks the signature and expiry and needs no database.
Whether the session has since been signed out is a separate question, answered
by ensure_active(), which the API asks on every request. The signing key comes
from SESSION_SECRET; when it is absent a random one is generated at import,
which means restarting the process invalidates every session. That is
deliberate — a predictable default secret is worse.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from typing import Optional

from cynea import db

log = logging.getLogger("cynea.auth")

try:
    import bcrypt
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Authentication needs bcrypt:  pip install bcrypt==4.0.1"
    ) from exc


SESSION_TTL_SECONDS = 60 * 60 * 24 * 7          # one week
MIN_PASSWORD_LENGTH = 8
_BCRYPT_ROUNDS = 12                              # ~250 ms per hash


class AuthError(Exception):
    """Base class for authentication failures."""


class InvalidCredentials(AuthError):
    """Wrong email or password. Deliberately does not say which."""


class EmailAlreadyRegistered(AuthError):
    pass


class WeakPassword(AuthError):
    pass


class EmailNotVerified(AuthError):
    """The password is right but the email address was never confirmed."""


class InvalidSession(AuthError):
    """Token missing, malformed, tampered with, or expired."""


# ----------------------------------------------------------------------
# Signing key
# ----------------------------------------------------------------------

def _session_secret() -> bytes:
    secret = os.getenv("SESSION_SECRET", "").strip()
    if secret:
        return secret.encode("utf-8")
    # No shared default: a hardcoded fallback would let anyone who has read
    # the source mint valid tokens against any deployment.
    if not hasattr(_session_secret, "_ephemeral"):
        _session_secret._ephemeral = secrets.token_bytes(32)
        log.warning(
            "SESSION_SECRET is not set; generated an ephemeral one. All "
            "sessions become invalid when this process restarts, and tokens "
            "will not validate across multiple workers. Set SESSION_SECRET "
            "in .env before deploying."
        )
    return _session_secret._ephemeral


# ----------------------------------------------------------------------
# Passwords
# ----------------------------------------------------------------------

def hash_password(password: str) -> str:
    """bcrypt hash, salt included in the output string."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    # bcrypt silently truncates past 72 bytes; reject rather than let a
    # user believe a long passphrase is fully protecting the account.
    if len(password.encode("utf-8")) > 72:
        raise WeakPassword("Password must be at most 72 bytes.")
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    ).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check. False on any malformed hash rather than raising."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ----------------------------------------------------------------------
# Registration and login
# ----------------------------------------------------------------------

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(\.[^@\s.]+)+$")


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    """A shape check, not proof: only the verification link proves an address."""
    return len(email) <= 320 and bool(_EMAIL.match(email))


def register_user(email: str, password: str, full_name: Optional[str] = None,
                  company: Optional[str] = None):
    """Create a user. Raises EmailAlreadyRegistered / WeakPassword.

    The account starts unverified; call issue_verification() to mail a link.
    """
    email = normalise_email(email)
    if not valid_email(email):
        raise AuthError("That doesn't look like an email address.")

    if db.get_user_by_email(email) is not None:
        raise EmailAlreadyRegistered("Email already registered")

    user = db.create_user(email, hash_password(password),
                          full_name=(full_name or "").strip() or None,
                          company=(company or "").strip() or None)
    log.info("registered user %s", email)
    return user


def login_user(email: str, password: str):
    """Return the user on success, else raise InvalidCredentials.

    The same error is raised for an unknown email and a wrong password, so
    the endpoint cannot be used to enumerate registered addresses. The
    dummy verify on the unknown-email path keeps the timing comparable.
    """
    email = (email or "").strip().lower()
    user = db.get_user_by_email(email)

    if user is None:
        verify_password(password, _DUMMY_HASH)
        raise InvalidCredentials("Incorrect email or password.")

    if not verify_password(password, user.password_hash):
        raise InvalidCredentials("Incorrect email or password.")

    return user


# Cost-matched dummy so a missing account takes the same time as a wrong
# password. Computed once at import.
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode()


def get_user_by_id(user_id: str):
    return db.get_user_by_id(user_id)


# ----------------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def create_session(user_id: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    """Mint a signed token for `user_id`."""
    payload = {
        "sub": str(user_id),
        # To the millisecond: session_cutoffs compares against it, and a new
        # token issued right after a password change must not read as older.
        "iat": round(time.time(), 3),
        "exp": int(time.time()) + int(ttl_seconds),
        "jti": secrets.token_urlsafe(8),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_session_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def session_claims(token: str) -> dict:
    """The verified contents of a token, else raise InvalidSession.

    Verifies the signature before parsing the payload, so a forged token
    never reaches json.loads.
    """
    if not token or "." not in token:
        raise InvalidSession("Malformed session token.")

    body, _, sig = token.partition(".")
    try:
        expected = hmac.new(_session_secret(), body.encode("ascii"), hashlib.sha256).digest()
        provided = _b64d(sig)
    except Exception:                      # non-ASCII is a bad token, not a 500
        raise InvalidSession("Malformed session token.")

    if not hmac.compare_digest(expected, provided):
        raise InvalidSession("Session signature does not verify.")

    try:
        payload = json.loads(_b64d(body))
    except Exception:
        raise InvalidSession("Malformed session payload.")

    if payload.get("exp", 0) < time.time():
        raise InvalidSession("Session has expired. Sign in again.")

    if not payload.get("sub"):
        raise InvalidSession("Session carries no user.")
    return payload


def validate_session(token: str) -> str:
    """Return the user_id carried by a valid token, else raise InvalidSession."""
    return str(session_claims(token)["sub"])


# ----------------------------------------------------------------------
# Signing out
# ----------------------------------------------------------------------

def ensure_active(claims: dict) -> str:
    """The user id, if this session is still good. Raises InvalidSession if the
    account is gone, it was signed out, or the password has changed since."""
    user_id = str(claims["sub"])
    with db.session_scope() as s:
        if not db._is_uuid(user_id) or s.get(db.User, user_id) is None:
            raise InvalidSession("The account for this session no longer exists.")
        if db.table_ready("revoked_tokens") and claims.get("jti") \
                and s.get(db.RevokedToken, str(claims["jti"])) is not None:
            raise InvalidSession("You have signed out. Sign in again.")
        if db.table_ready("session_cutoffs"):
            cutoff = s.get(db.SessionCutoff, user_id)
            if cutoff is not None and float(claims.get("iat", 0)) < cutoff.not_before:
                raise InvalidSession("Your password changed. Sign in again.")
    return user_id


def revoke_session(claims: dict) -> None:
    """Sign one session out, for as long as the token would otherwise work."""
    from sqlalchemy import delete

    now = time.time()
    with db.session_scope() as s:
        s.execute(delete(db.RevokedToken).where(db.RevokedToken.expires_at < now))
        if s.get(db.RevokedToken, str(claims["jti"])) is None:
            s.add(db.RevokedToken(jti=str(claims["jti"]), expires_at=float(claims["exp"])))


def revoke_all_sessions(user_id: str) -> None:
    """Every session issued up to now stops working."""
    now = time.time()
    with db.session_scope() as s:
        row = s.get(db.SessionCutoff, str(user_id))
        if row is None:
            s.add(db.SessionCutoff(user_id=str(user_id), not_before=now))
        else:
            row.not_before = now


# ----------------------------------------------------------------------
# Signed, single-purpose values
# ----------------------------------------------------------------------
# A call ticket is signed with the same key as a session but over a different
# string, so neither can ever be presented as the other.

def sign_value(purpose: str, data: dict, ttl_seconds: int) -> str:
    body = _b64e(json.dumps({**data, "exp": int(time.time()) + int(ttl_seconds)},
                            separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_session_secret(), f"{purpose}:{body}".encode("ascii"), hashlib.sha256)
    return f"{body}.{_b64e(sig.digest())}"


def read_value(purpose: str, token: str) -> Optional[dict]:
    """The data in a signed value, or None if it is forged or expired."""
    try:
        body, _, sig = (token or "").partition(".")
        want = hmac.new(_session_secret(), f"{purpose}:{body}".encode("ascii"),
                        hashlib.sha256).digest()
        if not body or not hmac.compare_digest(want, _b64d(sig)):
            return None
        data = json.loads(_b64d(body))
    except Exception:
        return None
    return data if data.get("exp", 0) >= time.time() else None


# ----------------------------------------------------------------------
# Email verification
# ----------------------------------------------------------------------
# The token is 32 random bytes, sent only in the email. The database keeps
# its sha256, so a leaked users table cannot be replayed into verifications.

VERIFY_TTL_SECONDS = 24 * 3600


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _aware(dt):
    """SQLite hands back naive datetimes for timezone=True columns."""
    from datetime import timezone
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_verified(user) -> bool:
    return user is not None and user.email_verified_at is not None


def issue_verification(user_id: str) -> str:
    """Store a fresh token hash with a 24h expiry; return the raw token.

    Issuing a new token replaces the old one, so a resend invalidates the
    previous email's link.
    """
    from datetime import timedelta

    raw = secrets.token_urlsafe(32)
    with db.session_scope() as s:
        user = s.get(db.User, str(user_id))
        if user is None:
            raise AuthError("No such account.")
        user.verify_token_hash = _token_hash(raw)
        user.verify_token_expires = db._now() + timedelta(seconds=VERIFY_TTL_SECONDS)
    return raw


def verify_email(raw: str):
    """Mark the account behind a token verified. Returns the user or None.

    None covers an unknown, already-used or expired token alike — the
    response must not tell a guesser which.
    """
    from sqlalchemy import select

    if not raw:
        return None
    with db.session_scope() as s:
        # _token_hash takes any string, so a junk token simply finds nobody.
        user = s.scalar(select(db.User).where(db.User.verify_token_hash == _token_hash(raw)))
        if user is None:
            return None
        if _aware(user.verify_token_expires) is None or _aware(user.verify_token_expires) < db._now():
            return None
        user.email_verified_at = db._now()
        user.verify_token_hash = None
        user.verify_token_expires = None
        return user


def require_verified(user) -> None:
    if not is_verified(user):
        raise EmailNotVerified("Please verify your email first.")


# ----------------------------------------------------------------------
# Passwords, after the fact
# ----------------------------------------------------------------------

RESET_TTL_SECONDS = 3600


def issue_password_reset(user_id: str) -> str:
    """A fresh reset token; any earlier link for this account stops working."""
    from datetime import timedelta

    from sqlalchemy import delete

    raw = secrets.token_urlsafe(32)
    with db.session_scope() as s:
        s.execute(delete(db.PasswordReset).where(db.PasswordReset.user_id == str(user_id)))
        s.add(db.PasswordReset(user_id=str(user_id), token_hash=_token_hash(raw),
                               expires_at=db._now() + timedelta(seconds=RESET_TTL_SECONDS)))
    return raw


def reset_password(raw: str, new_password: str):
    """Set a new password from an emailed link. Returns the user, or None for a
    bad, used or expired link.

    Following the link proves the mailbox. So this is also how an address is
    recovered from someone who registered it and never verified: the account
    becomes verified, and whatever that person typed in at sign-up goes.
    """
    from sqlalchemy import select

    password_hash = hash_password(new_password)        # WeakPassword before touching rows
    with db.session_scope() as s:
        row = s.scalar(select(db.PasswordReset)
                       .where(db.PasswordReset.token_hash == _token_hash(raw or "")))
        if row is None or row.used_at is not None or _aware(row.expires_at) < db._now():
            return None
        user = s.get(db.User, row.user_id)
        if user is None:
            return None
        row.used_at = db._now()
        user.password_hash = password_hash
        if user.email_verified_at is None:
            user.email_verified_at = db._now()
            user.full_name = None
            user.company = None
            user.verify_token_hash = None
            user.verify_token_expires = None
    revoke_all_sessions(user.id)
    return user


def change_password(user_id: str, current: str, new_password: str) -> None:
    """Change a password from inside a session. Every other session ends."""
    user = db.get_user_by_id(user_id)
    if user is None or not verify_password(current, user.password_hash):
        raise InvalidCredentials("Your current password is not right.")
    password_hash = hash_password(new_password)
    with db.session_scope() as s:
        s.get(db.User, str(user_id)).password_hash = password_hash
    revoke_all_sessions(user_id)


def current_user(token: str):
    """Resolve a token all the way to a User row.

    Costs one query, unlike validate_session. Use it only when the handler
    genuinely needs user fields; a token for a deleted account fails here.
    """
    user = db.get_user_by_id(validate_session(token))
    if user is None:
        raise InvalidSession("The account for this session no longer exists.")
    return user


__all__ = [
    "AuthError", "InvalidCredentials", "EmailAlreadyRegistered",
    "WeakPassword", "InvalidSession", "EmailNotVerified",
    "hash_password", "verify_password",
    "register_user", "login_user", "get_user_by_id",
    "create_session", "validate_session", "session_claims", "current_user",
    "ensure_active", "revoke_session", "revoke_all_sessions",
    "sign_value", "read_value", "normalise_email", "valid_email",
    "issue_verification", "verify_email", "is_verified", "require_verified",
    "issue_password_reset", "reset_password", "change_password",
    "VERIFY_TTL_SECONDS", "RESET_TTL_SECONDS",
    "SESSION_TTL_SECONDS", "MIN_PASSWORD_LENGTH",
]
