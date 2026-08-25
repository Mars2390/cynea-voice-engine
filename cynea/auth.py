"""Cynea Voice Engine — authentication.

Password hashing with bcrypt, plus signed stateless session tokens.

What this is
------------
Enough auth to put a real login in front of the console, replacing
`signin.html`'s redirect-and-hope. Passwords are bcrypt-hashed with a
per-password salt, and sessions are HMAC-signed tokens carrying an expiry.

What this is not
----------------
Not an identity provider. There is no password reset, no email
verification, no MFA, no OAuth, and no token revocation list — a stolen
token stays valid until it expires. For a first paying customer that is
an acceptable trade; before self-serve signup, move to Clerk or Auth0
(gap BE-3 in GAP_ANALYSIS.md) rather than growing this file.

Token format
------------
    base64url(payload_json) + "." + base64url(hmac_sha256(payload))

Stateless by design: validating a token needs no database round-trip, so
an API request that only needs `user_id` never touches Postgres. The
signing key comes from SESSION_SECRET; when it is absent a random one is
generated at import, which means restarting the process invalidates every
session. That is deliberate — a predictable default secret is worse.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
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

def register_user(email: str, password: str):
    """Create a user. Raises EmailAlreadyRegistered / WeakPassword."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise AuthError(f"{email!r} is not a valid email address.")

    if db.get_user_by_email(email) is not None:
        raise EmailAlreadyRegistered(f"{email} is already registered.")

    user = db.create_user(email, hash_password(password))
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
        "iat": int(time.time()),
        "exp": int(time.time()) + int(ttl_seconds),
        "jti": secrets.token_urlsafe(8),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_session_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def validate_session(token: str) -> str:
    """Return the user_id carried by a valid token, else raise InvalidSession.

    Verifies the signature before parsing the payload, so a forged token
    never reaches json.loads.
    """
    if not token or "." not in token:
        raise InvalidSession("Malformed session token.")

    body, _, sig = token.partition(".")
    expected = hmac.new(_session_secret(), body.encode("ascii"), hashlib.sha256).digest()
    try:
        provided = _b64d(sig)
    except Exception:
        raise InvalidSession("Malformed session token.")

    if not hmac.compare_digest(expected, provided):
        raise InvalidSession("Session signature does not verify.")

    try:
        payload = json.loads(_b64d(body))
    except Exception:
        raise InvalidSession("Malformed session payload.")

    if payload.get("exp", 0) < time.time():
        raise InvalidSession("Session has expired. Sign in again.")

    user_id = payload.get("sub")
    if not user_id:
        raise InvalidSession("Session carries no user.")
    return str(user_id)


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
    "WeakPassword", "InvalidSession",
    "hash_password", "verify_password",
    "register_user", "login_user", "get_user_by_id",
    "create_session", "validate_session", "current_user",
    "SESSION_TTL_SECONDS", "MIN_PASSWORD_LENGTH",
]
