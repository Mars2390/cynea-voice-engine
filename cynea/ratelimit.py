"""Fixed-window rate limiting backed by the database.

The API runs as many short-lived serverless instances, so an in-memory
counter would give every instance its own allowance. Each attempt is a row
in `rate_events` instead, keyed by a hash of the client identifier (never
the raw IP), and counted over the window.
"""

from __future__ import annotations

import hashlib
import os
from datetime import timedelta

from sqlalchemy import delete, func, select

from cynea import db


class RateLimited(Exception):
    """Too many attempts in the window."""


def _key(raw: str) -> str:
    # Salted with SESSION_SECRET so the stored hashes cannot be reversed by
    # hashing every IPv4 address.
    salt = os.getenv("SESSION_SECRET", "cynea-rate")
    return hashlib.sha256(f"{salt}:{raw}".encode()).hexdigest()


def hit(scope: str, client: str, limit: int, window_s: int) -> int:
    """Record one attempt; raise RateLimited if it exceeds `limit` per window.

    The attempt is recorded before the check, so hammering a limited
    endpoint keeps the window full rather than reopening it.
    """
    key = _key(client)
    since = db._now() - timedelta(seconds=window_s)
    with db.session_scope() as s:
        # Opportunistic cleanup keeps the table small without a cron job.
        s.execute(delete(db.RateEvent).where(db.RateEvent.created_at < db._now() - timedelta(days=1)))
        s.add(db.RateEvent(scope=scope, key_hash=key))
        s.flush()
        n = s.scalar(select(func.count(db.RateEvent.id)).where(
            db.RateEvent.scope == scope, db.RateEvent.key_hash == key,
            db.RateEvent.created_at >= since)) or 0
    if n > limit:
        raise RateLimited(f"{scope}: {n} attempts in {window_s}s (limit {limit})")
    return n
