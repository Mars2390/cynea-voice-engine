"""API keys: call the API from your own code, without a session.

A key is shown once, when it is created. Only its sha256 is stored, so a leaked
table cannot be replayed, and "lost it" means making a new one. A key acts as
its owner on agents, calls and the dashboard, but cannot touch the account:
password, deletion, export, keys and webhooks all need a signed-in session, so a
key that leaks cannot be used to lock its owner out or mint more keys.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import List, Optional, Tuple

from sqlalchemy import select

from cynea import db

PREFIX = "cynea_"
MAX_PER_USER = 10


def is_key(token: str) -> bool:
    return (token or "").startswith(PREFIX)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create(user_id: str, name: str) -> Tuple[db.ApiKey, str]:
    """The stored row and the raw key, which is not available again."""
    raw = PREFIX + secrets.token_urlsafe(32)
    with db.session_scope() as s:
        row = db.ApiKey(user_id=str(user_id), name=name, prefix=raw[:14],
                        key_hash=_hash(raw))
        s.add(row)
        s.flush()
        return row, raw


def list_for(user_id: str) -> List[db.ApiKey]:
    with db.session_scope() as s:
        return list(s.scalars(select(db.ApiKey)
                              .where(db.ApiKey.user_id == str(user_id),
                                     db.ApiKey.revoked_at.is_(None))
                              .order_by(db.ApiKey.created_at)))


def revoke(user_id: str, key_id: str) -> bool:
    if not db._is_uuid(key_id):
        return False
    with db.session_scope() as s:
        row = s.get(db.ApiKey, str(key_id))
        if row is None or str(row.user_id) != str(user_id) or row.revoked_at is not None:
            return False
        row.revoked_at = db._now()
        return True


def resolve(raw: str) -> Optional[str]:
    """The owner of a working key, else None."""
    if not db.table_ready("api_keys"):
        return None
    with db.session_scope() as s:
        row = s.scalar(select(db.ApiKey).where(db.ApiKey.key_hash == _hash(raw)))
        if row is None or row.revoked_at is not None:
            return None
        # A rejected account's keys stop with its sessions. Left alone rather
        # than revoked, so approving the account again brings them back.
        owner = s.get(db.User, row.user_id)
        if owner is None or owner.status == db.REJECTED:
            return None
        # Coarse on purpose: a busy key should not write on every request.
        now = db._now()
        last = row.last_used_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=now.tzinfo)
        if last is None or now - last > timedelta(minutes=5):
            row.last_used_at = now
        return str(row.user_id)
