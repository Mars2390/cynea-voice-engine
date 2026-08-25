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
from cynea import auth, dashboard_data, db

try:
    from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
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


app = FastAPI(
    title="Cynea Voice Engine API",
    version="0.2.0",
    description="Agents, calls and dashboard data for the Cynea console.",
    lifespan=lifespan,
)

# The console is a static file that may be served from anywhere during
# development. Tighten this to the real origin before production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(db.DatabaseNotConfigured)
async def _db_not_configured(request: Request, exc: db.DatabaseNotConfigured):
    return JSONResponse(
        status_code=503,
        content={"error": "database_unavailable", "detail": str(exc)},
    )


@app.exception_handler(auth.AuthError)
async def _auth_error(request: Request, exc: auth.AuthError):
    code = 401 if isinstance(exc, (auth.InvalidCredentials, auth.InvalidSession)) else 400
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

@app.post("/auth/register", response_model=TokenOut, status_code=201, tags=["auth"])
def register(body: RegisterIn):
    user = auth.register_user(body.email, body.password)
    return TokenOut(token=auth.create_session(user.id), user_id=user.id,
                    email=user.email, expires_in=auth.SESSION_TTL_SECONDS)


@app.post("/auth/login", response_model=TokenOut, tags=["auth"])
def login(body: LoginIn):
    user = auth.login_user(body.email, body.password)
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


__all__ = ["app"]
