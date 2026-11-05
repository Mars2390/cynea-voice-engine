"""Cynea Voice Engine — persistence layer (Neon Postgres via SQLAlchemy).

Everything the console shows is currently in-memory: agent edits die with
the browser tab and calls are never recorded. This module is the storage
those screens need.

Setup
-----
1. Create a project at https://neon.tech (the free tier is enough to start).
2. Copy the pooled connection string into .env:

       DATABASE_URL=postgresql://user:pass@ep-xxx.eu-central-1.aws.neon.tech/neondb?sslmode=require

3. Create the tables:

       python -m cynea.migrate

Design notes
------------
- **UUID primary keys.** Call IDs end up in URLs and support tickets;
  sequential integers leak volume and invite enumeration.
- **Money in integer cents.** `cost_cents`, never a float. Floating-point
  money is how billing reconciliation goes wrong.
- **Cascade deletes.** Removing an agent removes its calls and prompt
  versions. Do the deletion in one place rather than leaving orphans.
- **Engine-agnostic.** Nothing here imports CyneaEngine, so the storage
  layer can be tested and migrated without booting the voice stack.
- Works against SQLite too (`DATABASE_URL=sqlite:///cynea.db`), which is
  what the test suite uses — no Postgres needed to run tests.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, List, Optional

try:
    from sqlalchemy import (
        Column, DateTime, Float, ForeignKey, Integer, String, Text,
        create_engine, select,
    )
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker
    from sqlalchemy.types import JSON, TypeDecorator, CHAR
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The database layer needs SQLAlchemy and a driver:\n"
        "    pip install 'SQLAlchemy>=2.0' psycopg2-binary\n"
        f"(original error: {exc})"
    ) from exc


log = logging.getLogger("cynea.db")

Base = declarative_base()

_SESSION_FACTORY = None
_ENGINE = None


class DatabaseNotConfigured(RuntimeError):
    """Raised when DATABASE_URL is missing or unusable."""


# ----------------------------------------------------------------------
# Portable UUID column — native uuid on Postgres, 36-char string elsewhere
# ----------------------------------------------------------------------

class GUID(TypeDecorator):
    """UUID that also works on SQLite, so tests need no Postgres."""

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import UUID as PGUUID
            return dialect.type_descriptor(PGUUID(as_uuid=False))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        return None if value is None else str(value)

    def process_result_value(self, value, dialect):
        return None if value is None else str(value)


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------
# Tables
# ----------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(GUID, primary_key=True, default=_uuid)
    email = Column(String(320), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    # Sign-up details and email verification. All nullable so the columns
    # can be added to a live table; see upgrade_schema() for how accounts
    # that predate verification are treated.
    full_name = Column(String(120), nullable=True)
    company = Column(String(160), nullable=True)
    email_verified_at = Column(DateTime(timezone=True), nullable=True)
    # sha256 of the emailed token — the token itself is never stored.
    verify_token_hash = Column(String(64), nullable=True)
    verify_token_expires = Column(DateTime(timezone=True), nullable=True)

    agents = relationship("Agent", back_populates="user",
                          cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<User {self.email}>"


class Agent(Base):
    __tablename__ = "agents"

    id = Column(GUID, primary_key=True, default=_uuid)
    user_id = Column(GUID, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    name = Column(String(120), nullable=False)
    persona = Column(String(40), nullable=False)      # kwame|amina|kofi|maya
    voice_config = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    user = relationship("User", back_populates="agents")
    calls = relationship("Call", back_populates="agent",
                         cascade="all, delete-orphan")
    prompt_versions = relationship("PromptVersion", back_populates="agent",
                                   cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Agent {self.name} ({self.persona})>"


class Call(Base):
    __tablename__ = "calls"

    id = Column(GUID, primary_key=True, default=_uuid)
    agent_id = Column(GUID, ForeignKey("agents.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    caller_number = Column(String(32), nullable=False)
    duration_s = Column(Integer, nullable=False, default=0)
    transcript = Column(Text, nullable=True)
    sentiment_score = Column(Float, nullable=True)     # -1.0 .. +1.0
    cost_cents = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=False, default="resolved")
    created_at = Column(DateTime(timezone=True), default=_now,
                        nullable=False, index=True)
    # Where the call's audio lives ("r2://bucket/key" or "local://key"),
    # null when nothing was recorded. The row in call_recordings carries
    # the details; this is the quick "is there audio?" answer.
    audio_url = Column(Text, nullable=True)

    agent = relationship("Agent", back_populates="calls")
    recordings = relationship("CallRecording", back_populates="call",
                              cascade="all, delete-orphan")

    VALID_STATUSES = ("resolved", "escalated", "abandoned")

    def __repr__(self) -> str:
        return f"<Call {self.caller_number} {self.status} {self.duration_s}s>"


class CallRecording(Base):
    """One stored audio file for a call. See cynea/recordings.py."""
    __tablename__ = "call_recordings"

    id = Column(GUID, primary_key=True, default=_uuid)
    call_id = Column(GUID, ForeignKey("calls.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    storage_key = Column(String(512), nullable=False)
    content_type = Column(String(80), nullable=True)
    duration_s = Column(Integer, nullable=True)
    bytes = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=_now,
                        nullable=False, index=True)

    call = relationship("Call", back_populates="recordings")


class RateEvent(Base):
    """One rate-limited action (e.g. a sign-up attempt) from one client.

    In the database rather than in memory because the API runs as many
    short-lived serverless instances, and a per-process counter would let
    each one grant its own allowance. The key is a hash, not an address.
    """
    __tablename__ = "rate_events"

    id = Column(GUID, primary_key=True, default=_uuid)
    scope = Column(String(32), nullable=False, index=True)
    key_hash = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=_now,
                        nullable=False, index=True)


class RevokedToken(Base):
    """A session token signed out before it expired. Rows outlive their use by
    nothing: expired ones are removed whenever another token is revoked."""
    __tablename__ = "revoked_tokens"

    jti = Column(String(64), primary_key=True)
    expires_at = Column(Float, nullable=False)


class SessionCutoff(Base):
    """Sessions issued before this moment are dead: the password changed."""
    __tablename__ = "session_cutoffs"

    user_id = Column(GUID, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    not_before = Column(Float, nullable=False)


class PasswordReset(Base):
    """A password-reset link. The token itself is only ever in the email."""
    __tablename__ = "password_resets"

    id = Column(GUID, primary_key=True, default=_uuid)
    user_id = Column(GUID, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class ApiKey(Base):
    """A key for calling the API without a session. Only the hash is stored."""
    __tablename__ = "api_keys"

    id = Column(GUID, primary_key=True, default=_uuid)
    user_id = Column(GUID, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    name = Column(String(120), nullable=False)
    prefix = Column(String(20), nullable=False)        # enough to recognise it by
    key_hash = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class Webhook(Base):
    """Where to POST events. `secret` signs each delivery, so it has to be
    readable here, unlike a password or an API key."""
    __tablename__ = "webhooks"

    id = Column(GUID, primary_key=True, default=_uuid)
    user_id = Column(GUID, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    url = Column(String(2048), nullable=False)
    secret = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    last_status = Column(Integer, nullable=True)       # HTTP status, 0 for no answer
    last_delivery_at = Column(DateTime(timezone=True), nullable=True)


class PromptVersion(Base):
    __tablename__ = "prompt_versions"

    id = Column(GUID, primary_key=True, default=_uuid)
    agent_id = Column(GUID, ForeignKey("agents.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    version = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    agent = relationship("Agent", back_populates="prompt_versions")

    def __repr__(self) -> str:
        return f"<PromptVersion v{self.version}>"


# ----------------------------------------------------------------------
# Connection
# ----------------------------------------------------------------------

def get_database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise DatabaseNotConfigured(
            "DATABASE_URL is not set.\n"
            "  1. Create a free Postgres at https://neon.tech\n"
            "  2. Add the pooled connection string to .env:\n"
            "       DATABASE_URL=postgresql://user:pass@host/db?sslmode=require\n"
            "  3. Run: python -m cynea.migrate\n"
            "  (For local tests: DATABASE_URL=sqlite:///cynea.db)"
        )
    # SQLAlchemy 2.x dropped the bare postgres:// alias that some hosts emit.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def get_engine(url: Optional[str] = None, echo: bool = False):
    """Process-wide engine. Pooling is pre-ping'd because Neon idles
    connections out, and a stale socket surfaces mid-call otherwise."""
    global _ENGINE
    if _ENGINE is None:
        url = url or get_database_url()
        kwargs = {"echo": echo, "future": True}
        if not url.startswith("sqlite"):
            kwargs.update(pool_pre_ping=True, pool_recycle=280, pool_size=5,
                          max_overflow=10)
        try:
            _ENGINE = create_engine(url, **kwargs)
        except SQLAlchemyError as exc:
            raise DatabaseNotConfigured(f"Could not create engine: {exc}") from exc
    return _ENGINE


def get_session_factory():
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = sessionmaker(bind=get_engine(), expire_on_commit=False,
                                        future=True)
    return _SESSION_FACTORY


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on any error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_connection() -> None:
    """Drop cached engine/session factory. Used by tests switching URLs."""
    global _ENGINE, _SESSION_FACTORY
    if _ENGINE is not None:
        _ENGINE.dispose()
    _ENGINE = None
    _SESSION_FACTORY = None
    _READY_TABLES.clear()


def init_db(url: Optional[str] = None) -> None:
    """Create missing tables and add missing columns. Safe to re-run."""
    engine = get_engine(url) if url else get_engine()
    try:
        Base.metadata.create_all(engine)
        upgrade_schema(engine)
    except SQLAlchemyError as exc:
        raise DatabaseNotConfigured(
            f"Could not create tables: {exc}\n"
            "Check that DATABASE_URL points at a reachable database and that "
            "the user has CREATE rights."
        ) from exc


def upgrade_schema(engine=None) -> List[str]:
    """Add columns a model has gained to a table that already exists.

    `create_all` creates missing tables but never alters existing ones, so
    columns added to a model in a later release would otherwise be missing
    from the live database and every query that selects them would fail.
    Only additive, nullable columns are handled — anything else is a real
    migration and should be written as one. Returns "table.column" for each
    column it added.

    Accounts that existed before email verification are marked verified the
    moment the column is first added: they signed up under the old rules,
    and locking them out of their own workspace is not a security gain.
    """
    from sqlalchemy import inspect, text

    engine = engine or get_engine()
    insp = inspect(engine)
    added: List[str] = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                if not col.nullable:
                    raise DatabaseNotConfigured(
                        f"{table.name}.{col.name} is NOT NULL and missing from the "
                        "database; write a real migration for it.")
                ddl = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}"))
                added.append(f"{table.name}.{col.name}")
        if "users.email_verified_at" in added:
            n = conn.execute(text("UPDATE users SET email_verified_at = created_at "
                                  "WHERE email_verified_at IS NULL")).rowcount
            log.warning("email verification added: %s existing account(s) marked verified", n)
    if added:
        log.warning("schema upgraded, columns added: %s", ", ".join(added))
    return added


_READY_TABLES: set = set()


def table_ready(name: str) -> bool:
    """Does this table exist yet?

    The deployed API does not run migrations (Vercel's mount skips lifespan),
    so code can reach production minutes before its table does. Used only for
    checks that sit on every request, such as session revocation, so that gap
    cannot take down sign-in. Remembered once true; a table does not go away.
    """
    if name in _READY_TABLES:
        return True
    from sqlalchemy import inspect

    if inspect(get_engine()).has_table(name):
        _READY_TABLES.add(name)
        return True
    log.error("table %s is missing: run python -m cynea.migrate", name)
    return False


def _is_uuid(value) -> bool:
    """Postgres raises on a malformed uuid where SQLite just finds nothing, so
    a bad id in a URL was a 404 in tests and a 500 in production."""
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def healthcheck(retries: int = 3, backoff_s: float = 2.0) -> bool:
    """True when the database answers. Wire this into /health.

    Retries by default because Neon suspends idle compute: the first
    connection after a quiet period takes several seconds to wake the
    instance and a single-shot check reports a healthy database as down.
    Measured cold start on the free tier: ~3.7s.
    """
    import time as _time

    from sqlalchemy import text

    for attempt in range(1, retries + 1):
        try:
            with session_scope() as s:
                s.execute(text("SELECT 1"))
            if attempt > 1:
                log.info("database answered on attempt %d (cold start)", attempt)
            return True
        except Exception as exc:
            if attempt == retries:
                log.warning("database unreachable after %d attempts: %s", retries, exc)
                return False
            # A stale pooled socket cannot be reused across a suspend.
            reset_connection()
            _time.sleep(backoff_s)
    return False


# ----------------------------------------------------------------------
# Users
# ----------------------------------------------------------------------

def create_user(email: str, password_hash: str, full_name: Optional[str] = None,
                company: Optional[str] = None) -> User:
    """Create a user. Email is stored lowercase so lookups are consistent."""
    with session_scope() as s:
        user = User(email=email.strip().lower(), password_hash=password_hash,
                    full_name=full_name, company=company)
        s.add(user)
        s.flush()
        return user


def get_user_by_email(email: str) -> Optional[User]:
    with session_scope() as s:
        return s.scalar(select(User).where(User.email == email.strip().lower()))


def get_user_by_id(user_id: str) -> Optional[User]:
    if not _is_uuid(user_id):
        return None
    with session_scope() as s:
        return s.get(User, str(user_id))


def delete_user(user_id: str) -> bool:
    """Delete an account and everything it owns.

    Agents, calls, recordings' rows and prompt versions go by ORM cascade. The
    tables with no relationship on User are removed by hand, because SQLite
    does not enforce ON DELETE unless asked. Stored audio is not in the
    database: delete those objects first (see account.delete_account).
    """
    from sqlalchemy import delete as sql_delete

    with session_scope() as s:
        user = s.get(User, str(user_id))
        if user is None:
            return False
        for model in (SessionCutoff, PasswordReset, ApiKey, Webhook):
            if table_ready(model.__tablename__):
                s.execute(sql_delete(model).where(model.user_id == str(user_id)))
        s.delete(user)
        return True


# ----------------------------------------------------------------------
# Agents
# ----------------------------------------------------------------------

def create_agent(user_id: str, name: str, persona: str,
                 voice_config: Optional[dict] = None) -> Agent:
    with session_scope() as s:
        agent = Agent(user_id=str(user_id), name=name,
                      persona=persona.lower(), voice_config=voice_config or {})
        s.add(agent)
        s.flush()
        return agent


def get_agents_by_user(user_id: str) -> List[Agent]:
    with session_scope() as s:
        return list(s.scalars(
            select(Agent).where(Agent.user_id == str(user_id))
            .order_by(Agent.created_at)
        ))


def get_agent_by_id(agent_id: str) -> Optional[Agent]:
    if not _is_uuid(agent_id):
        return None
    with session_scope() as s:
        return s.get(Agent, str(agent_id))


def update_agent(agent_id: str, **kwargs) -> Optional[Agent]:
    """Update name / persona / voice_config. Unknown fields are rejected
    rather than silently ignored, so a typo fails loudly."""
    allowed = {"name", "persona", "voice_config"}
    unknown = set(kwargs) - allowed
    if unknown:
        raise ValueError(f"Cannot update {sorted(unknown)}; allowed: {sorted(allowed)}")

    with session_scope() as s:
        agent = s.get(Agent, str(agent_id))
        if agent is None:
            return None
        for key, value in kwargs.items():
            setattr(agent, key, value.lower() if key == "persona" else value)
        s.flush()
        return agent


def delete_agent(agent_id: str) -> bool:
    """Delete an agent and, by cascade, its calls and prompt versions."""
    with session_scope() as s:
        agent = s.get(Agent, str(agent_id))
        if agent is None:
            return False
        s.delete(agent)
        return True


# ----------------------------------------------------------------------
# Calls
# ----------------------------------------------------------------------

def log_call(agent_id: str, caller_number: str, duration: int,
             transcript: str = "", sentiment: Optional[float] = None,
             cost: int = 0, status: str = "resolved") -> Call:
    """Record a completed call.

    `cost` is integer cents. `sentiment` is -1.0..+1.0 or None when the
    call was too short to score.
    """
    if status not in Call.VALID_STATUSES:
        raise ValueError(
            f"status must be one of {Call.VALID_STATUSES}, got {status!r}"
        )
    if sentiment is not None and not (-1.0 <= sentiment <= 1.0):
        raise ValueError(f"sentiment must be between -1 and 1, got {sentiment}")
    if int(duration) < 0:
        raise ValueError(f"duration cannot be negative, got {duration}")
    if int(cost) < 0:
        raise ValueError(f"cost cannot be negative, got {cost}")

    with session_scope() as s:
        call = Call(agent_id=str(agent_id), caller_number=caller_number,
                    duration_s=int(duration), transcript=transcript or "",
                    sentiment_score=sentiment, cost_cents=int(cost),
                    status=status)
        s.add(call)
        s.flush()
        return call


def get_calls_by_agent(agent_id: str, limit: int = 100,
                       status: Optional[str] = None) -> List[Call]:
    """Most recent first — that is the order the console renders."""
    with session_scope() as s:
        stmt = select(Call).where(Call.agent_id == str(agent_id))
        if status:
            stmt = stmt.where(Call.status == status)
        stmt = stmt.order_by(Call.created_at.desc()).limit(limit)
        return list(s.scalars(stmt))


def get_call_by_id(call_id: str) -> Optional[Call]:
    if not _is_uuid(call_id):
        return None
    with session_scope() as s:
        return s.get(Call, str(call_id))


# ----------------------------------------------------------------------
# Prompt versions
# ----------------------------------------------------------------------

def save_prompt_version(agent_id: str, content: str) -> PromptVersion:
    """Append a new version, numbering it automatically from the highest
    existing version for that agent."""
    with session_scope() as s:
        latest = s.scalar(
            select(PromptVersion.version)
            .where(PromptVersion.agent_id == str(agent_id))
            .order_by(PromptVersion.version.desc()).limit(1)
        )
        version = PromptVersion(agent_id=str(agent_id),
                                version=(latest or 0) + 1, content=content)
        s.add(version)
        s.flush()
        return version


def get_prompt_versions(agent_id: str, limit: int = 50) -> List[PromptVersion]:
    """Newest first."""
    with session_scope() as s:
        return list(s.scalars(
            select(PromptVersion)
            .where(PromptVersion.agent_id == str(agent_id))
            .order_by(PromptVersion.version.desc()).limit(limit)
        ))


def get_latest_prompt(agent_id: str) -> Optional[PromptVersion]:
    versions = get_prompt_versions(agent_id, limit=1)
    return versions[0] if versions else None


__all__ = [
    "Base", "User", "Agent", "Call", "PromptVersion", "CallRecording", "RateEvent",
    "RevokedToken", "SessionCutoff", "PasswordReset", "ApiKey", "Webhook",
    "DatabaseNotConfigured", "init_db", "upgrade_schema", "healthcheck", "session_scope",
    "get_engine", "get_database_url", "reset_connection", "table_ready",
    "create_user", "get_user_by_email", "get_user_by_id", "delete_user",
    "create_agent", "get_agents_by_user", "get_agent_by_id",
    "update_agent", "delete_agent",
    "log_call", "get_calls_by_agent", "get_call_by_id",
    "save_prompt_version", "get_prompt_versions", "get_latest_prompt",
]
