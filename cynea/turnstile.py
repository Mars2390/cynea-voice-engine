"""Cloudflare Turnstile — the bot check on sign-up.

    TURNSTILE_SITE_KEY     public; the sign-up page reads it from /auth/config
    TURNSTILE_SECRET_KEY   private; used here to verify the widget's token

With no secret configured the check is skipped and a warning is logged once,
so local development and the test suite are not blocked. In production set
both, or sign-up is protected by the rate limit alone.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger("cynea.turnstile")

_VERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_warned = False


def site_key() -> Optional[str]:
    return os.getenv("TURNSTILE_SITE_KEY", "").strip() or None


def enabled() -> bool:
    return bool(os.getenv("TURNSTILE_SECRET_KEY", "").strip())


def verify(token: Optional[str], remote_ip: Optional[str] = None) -> bool:
    """True when the token is valid, or when Turnstile is not configured."""
    global _warned
    secret = os.getenv("TURNSTILE_SECRET_KEY", "").strip()
    if not secret:
        if not _warned:
            log.warning("TURNSTILE_SECRET_KEY not set — sign-up bot check is skipped")
            _warned = True
        return True
    if not token:
        return False

    import httpx

    data = {"secret": secret, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        r = httpx.post(_VERIFY, data=data, timeout=10.0)
        return bool(r.json().get("success"))
    except Exception as exc:
        # Fail closed: an unverifiable token is not a verified one.
        log.error("Turnstile verification failed: %s", exc)
        return False
