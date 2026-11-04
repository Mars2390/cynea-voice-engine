"""Cynea Voice Engine — HTTP API.

    uvicorn cynea.api:app --reload --port 8000
    open http://localhost:8000/docs

Auth
----
Bearer tokens from POST /auth/login. Every /agents and /calls route is
scoped to the authenticated user: you cannot read or modify another
workspace's rows even with a valid id, because ownership is checked on
every lookup rather than trusted from the URL.

Errors
------
JSON, always: {"error": "...", "detail": "..."}. Database outages return
503 rather than 500, so a client can tell "try again" from "you sent
something wrong".
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import List, Optional

import cynea  # noqa: F401  — loads .env
from cynea import auth, dashboard_data, db, mailer, ratelimit, recordings, turnstile

try:
    from fastapi import (Depends, FastAPI, File, Form, Header, HTTPException, Query,
                         Request, UploadFile, status)
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
    from pydantic import BaseModel, EmailStr, Field
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The API needs FastAPI:  pip install fastapi==0.115.6 uvicorn==0.34.0"
    ) from exc

log = logging.getLogger("cynea.api")


# ----------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------

class RegisterIn(BaseModel):
    email: str = Field(..., examples=["ama@adinkra.example"])
    password: str = Field(..., min_length=auth.MIN_PASSWORD_LENGTH)
    name: Optional[str] = Field(None, max_length=120)
    company: Optional[str] = Field(None, max_length=160)
    # Cloudflare Turnstile widget token; required only when Turnstile is on.
    turnstile_token: Optional[str] = Field(None, max_length=4096)


class RegisterOut(BaseModel):
    status: str
    email: str


class ResendIn(BaseModel):
    email: str


class LoginIn(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    token: str
    user_id: str
    email: str
    expires_in: int


class AgentIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    persona: str = Field(..., examples=["kwame"])
    voice_config: dict = Field(default_factory=dict)


class AgentUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    persona: Optional[str] = None
    voice_config: Optional[dict] = None


class CallIn(BaseModel):
    agent_id: str
    caller_number: str = Field(..., min_length=1,
                               description="Caller's number from the telephony layer")
    duration_s: int = 0
    transcript: str = ""
    sentiment_score: Optional[float] = Field(None, ge=-1.0, le=1.0)
    cost_cents: int = 0
    status: str = "resolved"


# ----------------------------------------------------------------------
# App
# ----------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Report connection status loudly at boot rather than on first request."""
    print("\n" + "=" * 62)
    print("  CYNEA VOICE ENGINE - API")
    print("=" * 62)
    try:
        url = db.get_database_url()
        masked = url.split("@")[-1] if "@" in url else url
        print(f"  database   : {masked[:56]}")
        ok = db.healthcheck()
        print(f"  connected  : {'yes' if ok else 'NO'}")
        if ok:
            db.init_db()
            with db.session_scope() as s:
                from sqlalchemy import func, select
                users = s.scalar(select(func.count(db.User.id))) or 0
                agents = s.scalar(select(func.count(db.Agent.id))) or 0
                calls = s.scalar(select(func.count(db.Call.id))) or 0
            print(f"  rows       : {users} users, {agents} agents, {calls} calls")
        else:
            print("  -> Neon suspends idle compute; the first request may wake it.")
    except db.DatabaseNotConfigured as exc:
        print(f"  database   : NOT CONFIGURED\n{exc}")
    print(f"  providers  : {cynea.providers.registered()}")
    print(f"  docs       : http://localhost:{os.getenv('PORT', '8000')}/docs")
    print("=" * 62 + "\n")
    yield


# In production the interactive docs are off and the schema needs a session:
# the route list is a map of the attack surface, and a public Swagger UI is
# also a public "try it" button against the live database.
PRODUCTION = os.getenv("ENV", "").strip().lower() == "production"

app = FastAPI(
    title="Cynea Voice Engine API",
    version="0.3.0",
    description="Agents, calls and dashboard data for the Cynea console.",
    lifespan=lifespan,
    docs_url=None if PRODUCTION else "/docs",
    redoc_url=None if PRODUCTION else "/redoc",
    openapi_url=None if PRODUCTION else "/openapi.json",
)

# ----------------------------------------------------------------------
# CORS
# ----------------------------------------------------------------------
# The default used to be "*", which is the one setting that must not be a
# default: `allow_credentials=True` with a wildcard origin means any site a
# logged-in user visits can call this API with their bearer token attached.
# Browsers reject that combination outright, so the practical effect was a
# console that worked locally and failed the moment it had a real origin.
#
# The default below covers the two places the console actually runs — a
# laptop and a Vercel deployment — and CORS_ORIGINS overrides it with a
# comma-separated list once there is a custom domain.
DEFAULT_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8000",
    "http://localhost:8080",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8000",
    "http://127.0.0.1:8080",
]

_origins_env = os.getenv("CORS_ORIGINS", "").strip()
_allow_origins = ([o.strip() for o in _origins_env.split(",") if o.strip()]
                  if _origins_env else DEFAULT_ORIGINS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    # Every *.vercel.app preview and the production domain, without having
    # to list a deployment hash that changes on every push.
    allow_origin_regex=r"https://([a-z0-9-]+\.)*vercel\.app|https://([a-z0-9-]+\.)*cynea\.ai",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# ── the browser call demo ───────────────────────────────────────────────
# Registered here rather than in its own app so it shares this one's CORS
# and lifespan. Guarded: the socket needs the voice providers, and a
# deployment without them should still serve agents and the dashboard
# rather than fail to import.
try:
    from cynea import voice_ws
    voice_ws.register(app)
except Exception as _exc:                                  # pragma: no cover
    log.warning("live voice demo unavailable: %s", _exc)


@app.exception_handler(db.DatabaseNotConfigured)
async def _db_not_configured(request: Request, exc: db.DatabaseNotConfigured):
    return JSONResponse(
        status_code=503,
        content={"error": "database_unavailable", "detail": str(exc)},
    )


@app.exception_handler(auth.AuthError)
async def _auth_error(request: Request, exc: auth.AuthError):
    code = (401 if isinstance(exc, (auth.InvalidCredentials, auth.InvalidSession))
            else 403 if isinstance(exc, auth.EmailNotVerified) else 400)
    return JSONResponse(
        status_code=code,
        content={"error": type(exc).__name__, "detail": str(exc)},
    )


# ----------------------------------------------------------------------
# Auth dependency
# ----------------------------------------------------------------------

def current_user_id(authorization: str = Header(default="")) -> str:
    """Resolve `Authorization: Bearer <token>` to a user id."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token. Sign in at POST /auth/login.")
    try:
        return auth.validate_session(authorization[7:].strip())
    except auth.InvalidSession as exc:
        raise HTTPException(401, str(exc)) from exc


def _own_agent(agent_id: str, user_id: str):
    """Fetch an agent, 404ing if it is missing *or* not yours.

    Deliberately the same response either way: a different code would let
    a caller probe which agent ids exist in other workspaces.
    """
    agent = db.get_agent_by_id(agent_id)
    if agent is None or str(agent.user_id) != str(user_id):
        raise HTTPException(404, "Agent not found.")
    return agent


def _client_ip(request: Request) -> str:
    """The caller's address. Vercel puts the real one first in X-Forwarded-For."""
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else "") or (
        request.client.host if request.client else "unknown")


def _api_base(request: Request) -> str:
    """This API's public base URL, ending in "/" — "https://host/api/" on Vercel,
    where the app is mounted under /api and Starlette puts that in root_path."""
    explicit = os.getenv("PUBLIC_API_URL", "").strip().rstrip("/")
    return explicit + "/" if explicit else str(request.base_url)


def _site_base(request: Request) -> str:
    """Where the static pages live: SITE_URL, else the API's origin."""
    explicit = os.getenv("SITE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    base = _api_base(request).rstrip("/")
    return base[:-4] if base.endswith("/api") else base


def _send_verification(request: Request, user) -> str:
    raw = auth.issue_verification(user.id)
    link = f"{_api_base(request)}auth/verify?token={raw}"
    return mailer.send_verification(user.email, link, user.full_name or "")


REGISTER_LIMIT = int(os.getenv("REGISTER_RATE_LIMIT", "5"))      # per IP per hour


def _own_call(call_id: str, user_id: str):
    """The call, 404ing if it is missing or belongs to another workspace."""
    call = db.get_call_by_id(call_id)
    if call is None:
        raise HTTPException(404, "Call not found.")
    try:
        _own_agent(call.agent_id, user_id)
    except HTTPException:
        raise HTTPException(404, "Call not found.") from None
    return call


def _agent_json(a) -> dict:
    meta = dashboard_data.PERSONA_META.get(a.persona, {})
    return {
        "id": a.id, "name": a.name, "persona": a.persona,
        "voice_config": a.voice_config or {},
        "role": meta.get("role"), "place": meta.get("place"),
        "photo": meta.get("photo"),
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


# ----------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------

@app.get("/health", tags=["system"])
def health():
    ok = db.healthcheck(retries=1)
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "status": "ok" if ok else "degraded",
            "database": ok,
            "providers": cynea.providers.registered(),
        },
    )


# ----------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------

@app.get("/auth/config", tags=["auth"])
def auth_config():
    """Public settings the sign-up page needs before it can render."""
    return {"turnstile_site_key": turnstile.site_key(), "email_verification": True}


@app.post("/auth/register", response_model=RegisterOut, status_code=201, tags=["auth"])
def register(body: RegisterIn, request: Request):
    """Create an unverified account and email a verification link.

    Does not sign the user in: /auth/login refuses the account until the
    link is followed.
    """
    ip = _client_ip(request)
    try:
        ratelimit.hit("register", ip, limit=REGISTER_LIMIT, window_s=3600)
    except ratelimit.RateLimited:
        raise HTTPException(429, "Too many sign-up attempts from this network. "
                                 "Try again in an hour.") from None
    if not turnstile.verify(body.turnstile_token, ip):
        raise HTTPException(400, "Bot check failed. Please try again.")

    user = auth.register_user(body.email, body.password,
                              full_name=body.name, company=body.company)
    try:
        _send_verification(request, user)
    except mailer.EmailError as exc:
        log.error("verification email for %s failed: %s", user.email, exc)
        raise HTTPException(503, "Your account was created, but the verification email "
                                 "could not be sent. Use 'Send a new link' on the sign-in "
                                 "page in a few minutes.") from None
    return RegisterOut(status="verification_sent", email=user.email)


@app.get("/auth/verify", tags=["auth"], include_in_schema=False)
def verify(request: Request, token: str = Query("", max_length=256)):
    """The emailed link. Verifies, then sends the browser to sign-in."""
    user = auth.verify_email(token)
    outcome = "verified=1" if user else "verify=invalid"
    return RedirectResponse(f"{_site_base(request)}/signin.html?{outcome}", status_code=303)


@app.post("/auth/resend-verification", status_code=202, tags=["auth"])
def resend_verification(body: ResendIn, request: Request):
    """Mail a new link. Always the same answer, so it reveals no accounts."""
    try:
        ratelimit.hit("resend", _client_ip(request), limit=REGISTER_LIMIT, window_s=3600)
    except ratelimit.RateLimited:
        raise HTTPException(429, "Too many requests from this network. "
                                 "Try again in an hour.") from None
    user = db.get_user_by_email((body.email or "").strip().lower())
    if user is not None and not auth.is_verified(user):
        try:
            _send_verification(request, user)
        except mailer.EmailError as exc:
            log.error("verification resend for %s failed: %s", user.email, exc)
    return {"status": "sent_if_registered"}


@app.post("/auth/login", response_model=TokenOut, tags=["auth"])
def login(body: LoginIn):
    user = auth.login_user(body.email, body.password)
    auth.require_verified(user)          # 403 "Please verify your email first."
    return TokenOut(token=auth.create_session(user.id), user_id=user.id,
                    email=user.email, expires_in=auth.SESSION_TTL_SECONDS)


@app.get("/auth/me", tags=["auth"])
def me(user_id: str = Depends(current_user_id)):
    user = db.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(401, "The account for this session no longer exists.")
    return {"id": user.id, "email": user.email,
            "created_at": user.created_at.isoformat() if user.created_at else None}


# ----------------------------------------------------------------------
# Agents
# ----------------------------------------------------------------------

@app.post("/agents", status_code=201, tags=["agents"])
def create_agent(body: AgentIn, user_id: str = Depends(current_user_id)):
    from cynea.agent_loader import _PERSONAS
    if body.persona.lower() not in _PERSONAS:
        raise HTTPException(
            400, f"Unknown persona {body.persona!r}. Available: {sorted(_PERSONAS)}"
        )
    voice = body.voice_config or dict(_PERSONAS[body.persona.lower()]["voice"])
    agent = db.create_agent(user_id, body.name, body.persona, voice)
    return _agent_json(agent)


@app.get("/agents", tags=["agents"])
def list_agents(user_id: str = Depends(current_user_id)):
    return [_agent_json(a) for a in db.get_agents_by_user(user_id)]


@app.get("/agents/{agent_id}", tags=["agents"])
def get_agent(agent_id: str, user_id: str = Depends(current_user_id)):
    agent = _own_agent(agent_id, user_id)
    data = _agent_json(agent)
    latest = db.get_latest_prompt(agent_id)
    data["prompt"] = latest.content if latest else None
    data["prompt_version"] = latest.version if latest else 0
    data["calls"] = len(db.get_calls_by_agent(agent_id, limit=500))
    return data


@app.put("/agents/{agent_id}", tags=["agents"])
def update_agent(agent_id: str, body: AgentUpdate,
                 user_id: str = Depends(current_user_id)):
    _own_agent(agent_id, user_id)
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items()
              if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update.")
    try:
        agent = db.update_agent(agent_id, **fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _agent_json(agent)


@app.delete("/agents/{agent_id}", status_code=204, tags=["agents"])
def delete_agent(agent_id: str, user_id: str = Depends(current_user_id)):
    _own_agent(agent_id, user_id)
    db.delete_agent(agent_id)
    return None


@app.post("/agents/{agent_id}/prompt", status_code=201, tags=["agents"])
def save_prompt(agent_id: str, body: dict, user_id: str = Depends(current_user_id)):
    """Append a prompt version. This is what the editor's Save button needs."""
    _own_agent(agent_id, user_id)
    content = (body or {}).get("content", "").strip()
    if not content:
        raise HTTPException(400, "content is required.")
    v = db.save_prompt_version(agent_id, content)
    return {"id": v.id, "version": v.version,
            "created_at": v.created_at.isoformat() if v.created_at else None}


@app.get("/agents/{agent_id}/prompts", tags=["agents"])
def list_prompts(agent_id: str, user_id: str = Depends(current_user_id)):
    _own_agent(agent_id, user_id)
    return [
        {"id": v.id, "version": v.version, "content": v.content,
         "created_at": v.created_at.isoformat() if v.created_at else None}
        for v in db.get_prompt_versions(agent_id)
    ]


# ----------------------------------------------------------------------
# Calls
# ----------------------------------------------------------------------

@app.post("/calls", status_code=201, tags=["calls"])
def log_call(body: CallIn, user_id: str = Depends(current_user_id)):
    _own_agent(body.agent_id, user_id)
    try:
        call = db.log_call(
            agent_id=body.agent_id, caller_number=body.caller_number,
            duration=body.duration_s, transcript=body.transcript,
            sentiment=body.sentiment_score, cost=body.cost_cents,
            status=body.status,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": call.id, "status": call.status,
            "created_at": call.created_at.isoformat() if call.created_at else None}


@app.get("/calls", tags=["calls"])
def list_calls(agent_id: Optional[str] = Query(None),
               limit: int = Query(50, ge=1, le=500),
               user_id: str = Depends(current_user_id)):
    if agent_id:
        _own_agent(agent_id, user_id)
        return dashboard_data.get_call_history(agent_id=agent_id, limit=limit)
    return dashboard_data.get_call_history(user_id=user_id, limit=limit)


@app.get("/calls/{call_id}", tags=["calls"])
def get_call(call_id: str, user_id: str = Depends(current_user_id)):
    call = db.get_call_by_id(call_id)
    if call is None:
        raise HTTPException(404, "Call not found.")
    _own_agent(call.agent_id, user_id)      # ownership via the parent agent
    detail = dashboard_data.get_call_detail(call_id)
    if detail is None:
        raise HTTPException(404, "Call not found.")
    return detail


# ----------------------------------------------------------------------
# Recordings
# ----------------------------------------------------------------------

@app.post("/calls/{call_id}/recording", status_code=201, tags=["calls"])
async def upload_recording(call_id: str, file: UploadFile = File(...),
                           duration_s: Optional[int] = Form(None, ge=0),
                           user_id: str = Depends(current_user_id)):
    """Attach the call's audio. A second upload replaces the first."""
    call = _own_call(call_id, user_id)
    ctype = (file.content_type or "").split(";")[0].strip().lower()
    if ctype not in recordings.CONTENT_TYPES:
        raise HTTPException(415, f"Unsupported audio type {ctype or 'unknown'!r}.")
    data = await file.read(recordings.MAX_BYTES + 1)
    if not data:
        raise HTTPException(400, "The upload was empty.")
    if len(data) > recordings.MAX_BYTES:
        raise HTTPException(413, f"Recording is over {recordings.MAX_BYTES // (1024 * 1024)} MB.")
    try:
        key, audio_url = recordings.store(call.id, data, ctype)
    except recordings.StorageError as exc:
        raise HTTPException(503, str(exc)) from exc

    with db.session_scope() as s:
        row = s.get(db.Call, call.id)
        old = [r.storage_key for r in row.recordings]
        previous_url = row.audio_url
        for r in list(row.recordings):
            s.delete(r)
        s.add(db.CallRecording(call_id=call.id, storage_key=key, content_type=ctype,
                               duration_s=duration_s, bytes=len(data)))
        row.audio_url = audio_url
    if previous_url and previous_url != audio_url:
        try:
            recordings.delete(previous_url)
        except recordings.StorageError as exc:
            log.warning("could not delete replaced recording %s: %s", old, exc)
    return {"call_id": call.id, "bytes": len(data), "duration_s": duration_s,
            "content_type": ctype, "storage": recordings.backend()}


@app.get("/calls/{call_id}/recording", tags=["calls"])
def get_recording(call_id: str, request: Request, user_id: str = Depends(current_user_id)):
    """A short-lived URL the browser can play, or 404 when there is no audio."""
    call = _own_call(call_id, user_id)
    if not call.audio_url:
        raise HTTPException(404, "No recording available")
    with db.session_scope() as s:
        from sqlalchemy import select
        rec = s.scalar(select(db.CallRecording).where(db.CallRecording.call_id == call.id)
                       .order_by(db.CallRecording.created_at.desc()))
        meta = ({"duration_s": rec.duration_s, "bytes": rec.bytes,
                 "content_type": rec.content_type} if rec else {})
    try:
        url = recordings.playback_url(call.audio_url, call.id, _api_base(request))
    except recordings.StorageError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"url": url, "expires_in": recordings.URL_TTL_S, **meta}


@app.get("/calls/{call_id}/recording/file", tags=["calls"], include_in_schema=False)
def recording_file(call_id: str, exp: int = Query(...), sig: str = Query(...)):
    """Local-disk playback. Authorised by the signed query string, because an
    <audio> element cannot send a bearer token."""
    if not recordings.check_signature(call_id, exp, sig):
        raise HTTPException(403, "This playback link is invalid or has expired.")
    call = db.get_call_by_id(call_id)
    if call is None or not (call.audio_url or "").startswith("local://"):
        raise HTTPException(404, "No recording available")
    path = recordings.local_file(call.audio_url)
    if not path.is_file():
        raise HTTPException(404, "No recording available")
    with db.session_scope() as s:
        from sqlalchemy import select
        rec = s.scalar(select(db.CallRecording).where(db.CallRecording.call_id == call.id))
        ctype = rec.content_type if rec and rec.content_type else "audio/mpeg"
    return FileResponse(path, media_type=ctype)


# ----------------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------------

@app.get("/dashboard/stats", tags=["dashboard"])
def dashboard_stats(user_id: str = Depends(current_user_id)):
    return dashboard_data.get_dashboard_stats(user_id=user_id)


@app.get("/dashboard/queue", tags=["dashboard"])
def dashboard_queue(user_id: str = Depends(current_user_id)):
    return dashboard_data.get_live_queue(user_id=user_id)


@app.get("/dashboard/bootstrap", tags=["dashboard"])
def dashboard_bootstrap(user_id: str = Depends(current_user_id)):
    """Everything the console needs on load, in one round trip.

    The dashboard otherwise fires four requests before it can paint; on a
    Nairobi mobile connection that is four round trips of latency, plus a
    possible Neon cold start on each.
    """
    return {
        "stats": dashboard_data.get_dashboard_stats(user_id=user_id),
        "queue": dashboard_data.get_live_queue(user_id=user_id),
        "calls": dashboard_data.get_call_history(user_id=user_id, limit=25),
    }


if PRODUCTION:
    @app.get("/openapi.json", include_in_schema=False)
    def openapi_schema(user_id: str = Depends(current_user_id)):
        """The schema, for signed-in callers only."""
        return JSONResponse(app.openapi())


__all__ = ["app"]
