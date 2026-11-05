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
from cynea import (account, apikeys, auth, call_config, dashboard_data, db, mailer,
                   monitoring, ratelimit, recordings, turnstile, twilio_voice, webhooks)

try:
    from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException,
                         Query, Request, UploadFile, status)
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
    from pydantic import BaseModel, EmailStr, Field
    from sqlalchemy.exc import SQLAlchemyError
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


class ForgotIn(BaseModel):
    email: str


class ResetIn(BaseModel):
    token: str = Field(..., max_length=256)
    password: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class DeleteAccountIn(BaseModel):
    password: str
    # Typed out on purpose, so that no client can do this by accident.
    confirm: str = Field(..., description='Must be exactly "DELETE".')


class ApiKeyIn(BaseModel):
    name: str = Field(..., max_length=120)


class WebhookIn(BaseModel):
    url: str = Field(..., max_length=2048)


class ClientErrorIn(BaseModel):
    message: str = Field(..., max_length=2000)
    page: Optional[str] = Field(None, max_length=200)


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
    paused: Optional[bool] = None


class PromptIn(BaseModel):
    content: str = Field(..., max_length=100_000)


class CallIn(BaseModel):
    agent_id: str
    caller_number: str = Field(..., min_length=1,
                               description="Caller's number from the telephony layer")
    duration_s: int = Field(0, ge=0)
    transcript: str = ""
    sentiment_score: Optional[float] = Field(None, ge=-1.0, le=1.0)
    cost_cents: int = Field(0, ge=0)
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


@app.exception_handler(SQLAlchemyError)
async def _database_error(request: Request, exc: SQLAlchemyError):
    """A database that is down or not migrated is "try again", not "you broke it"."""
    log.error("database error on %s: %s", request.url.path, type(exc).__name__)
    monitoring.capture(exc, tags={"path": request.url.path})
    return JSONResponse(
        status_code=503,
        content={"error": "database_unavailable",
                 "detail": "The database did not answer. Try again in a moment."},
    )


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """Anything nothing else caught: report it, and tell the client nothing."""
    log.exception("unhandled error on %s", request.url.path)
    monitoring.capture(exc, tags={"path": request.url.path})
    return JSONResponse(status_code=500,
                        content={"error": "internal_error",
                                 "detail": "Something went wrong on our side."})


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

def _bearer(authorization: str) -> str:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token. Sign in at POST /auth/login.")
    return authorization[7:].strip()


def _active_session(token: str) -> dict:
    """The claims of a session that is still good, else 401."""
    try:
        claims = auth.session_claims(token)
        auth.ensure_active(claims)
        return claims
    except auth.InvalidSession as exc:
        raise HTTPException(401, str(exc)) from exc


def current_user_id(authorization: str = Header(default="")) -> str:
    """Resolve `Authorization: Bearer <session token or API key>` to a user id."""
    token = _bearer(authorization)
    if apikeys.is_key(token):
        user_id = apikeys.resolve(token)
        if user_id is None:
            raise HTTPException(401, "This API key is not valid or has been revoked.")
        return user_id
    return str(_active_session(token)["sub"])


def session_claims(authorization: str = Header(default="")) -> dict:
    """For routes that change the account itself: an API key is not enough."""
    token = _bearer(authorization)
    if apikeys.is_key(token):
        raise HTTPException(403, "This needs a signed-in session, not an API key.")
    return _active_session(token)


def session_user_id(claims: dict = Depends(session_claims)) -> str:
    return str(claims["sub"])


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


class LinksNotConfigured(RuntimeError):
    """Production with no idea what its own address is."""


@app.exception_handler(LinksNotConfigured)
async def _links_not_configured(request: Request, exc: LinksNotConfigured):
    log.error("%s", exc)
    return JSONResponse(status_code=503, content={
        "error": "not_configured",
        "detail": "This server is not set up to send links yet. Try again later."})


# Links in emails are built from configuration and never from the request. The
# Host header is whatever the sender typed, so building from it would let anyone
# ask for a password-reset email whose link points at their own server.
#
# Vercel supplies the production domain itself, which is equally out of a
# caller's reach, so a deployment works before SITE_URL is set. Off Vercel and
# outside production there is only a laptop to point at. Production with nothing
# configured refuses.
def _vercel_origin() -> str:
    host = os.getenv("VERCEL_PROJECT_PRODUCTION_URL", "").strip()
    return f"https://{host}" if host else ""


def _is_deployed() -> bool:
    return PRODUCTION or bool(os.getenv("VERCEL"))


def _site_base() -> str:
    """Where the static pages live, without a trailing slash."""
    base = os.getenv("SITE_URL", "").strip().rstrip("/") or _vercel_origin()
    if base:
        return base
    if _is_deployed():
        raise LinksNotConfigured("SITE_URL is not set, so no link can be built.")
    return "http://127.0.0.1:8080"


def _api_base() -> str:
    """This API's public base URL, ending in "/" ("https://host/api/" on Vercel)."""
    explicit = os.getenv("PUBLIC_API_URL", "").strip().rstrip("/")
    if explicit:
        return explicit + "/"
    if os.getenv("SITE_URL", "").strip() and _is_deployed():
        return os.getenv("SITE_URL").strip().rstrip("/") + "/api/"
    if _vercel_origin():
        return _vercel_origin() + "/api/"
    if _is_deployed():
        raise LinksNotConfigured("PUBLIC_API_URL is not set, so no link can be built.")
    return f"http://127.0.0.1:{os.getenv('PORT', '8000')}/"


def _send_verification(user) -> str:
    link = f"{_api_base()}auth/verify?token={auth.issue_verification(user.id)}"
    return mailer.send_verification(user.email, link, user.full_name or "")


def _send_reset_quietly(user) -> None:
    """Runs after the response, so how long it takes says nothing about whether
    the address has an account."""
    try:
        link = f"{_site_base()}/signin.html?reset={auth.issue_password_reset(user.id)}"
        mailer.send_password_reset(user.email, link)
    except (mailer.EmailError, LinksNotConfigured) as exc:
        log.error("password reset email for %s failed: %s", user.email, exc)


def _resend_quietly(user) -> None:
    try:
        _send_verification(user)
    except (mailer.EmailError, LinksNotConfigured) as exc:
        log.error("verification resend for %s failed: %s", user.email, exc)


REGISTER_LIMIT = int(os.getenv("REGISTER_RATE_LIMIT", "5"))      # per IP per hour
LOGIN_LIMIT = int(os.getenv("LOGIN_RATE_LIMIT", "10"))           # failures, address + IP
LOGIN_WINDOW_S = 900
EMAIL_PER_ADDRESS = 3                                            # emails per address per hour


def _voice_options() -> list:
    from cynea_africa.synthesizer.edge_tts import EdgeTTSSynthesizer

    return [{"id": v, "label": d} for v, d in EdgeTTSSynthesizer.VOICES.items()]


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "Give the agent a name.")
    return name


def _known_persona(persona: str) -> str:
    from cynea.agent_loader import _PERSONAS

    persona = (persona or "").strip().lower()
    if persona not in _PERSONAS:
        raise HTTPException(400, f"Unknown persona {persona!r}. Available: {sorted(_PERSONAS)}")
    return persona


def _clean_voice_config(cfg: dict) -> dict:
    """Only what a call actually reads, and only voices that exist."""
    out = {}
    voice = cfg.get("voice")
    if voice:
        if voice not in {o["id"] for o in _voice_options()}:
            raise HTTPException(400, f"Unknown voice {voice!r}. See GET /agents/options.")
        out.update(provider="edge_tts", voice=voice)
    if cfg.get("speed") is not None:
        try:
            speed = float(cfg["speed"])
        except (TypeError, ValueError):
            raise HTTPException(400, "speed has to be a number.") from None
        if not 0.5 <= speed <= 2.0:
            raise HTTPException(400, "speed has to be between 0.5 and 2.")
        out["speed"] = speed
    if cfg.get("first_message") is not None:
        text = str(cfg["first_message"]).strip()
        if len(text) > 500:
            raise HTTPException(400, "The greeting can be at most 500 characters.")
        out["first_message"] = text
    return out


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
        "persona_name": meta.get("name") or a.persona.title(),
        "voice_config": a.voice_config or {},
        "voice": dashboard_data.agent_voice(a)["voice"],
        "paused": call_config.is_paused(a),
        "role": meta.get("role"),
        "photo": meta.get("photo"),
        "created_at": dashboard_data._iso(a.created_at),
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
            # Which settings are present, never their values. Lets a deployment
            # be checked from outside without anyone reading its secrets.
            "configured": {
                "production": PRODUCTION,
                "session_secret": bool(os.getenv("SESSION_SECRET", "").strip()),
                "email": bool(os.getenv("EMAIL_API_KEY", "").strip()),
                "site_url": bool(os.getenv("SITE_URL", "").strip()),
                "bot_check": bool(turnstile.site_key()),
                "recordings": recordings.backend(),
                "error_tracking": monitoring.endpoint() is not None,
                "twilio": twilio_voice.is_configured(),
            },
        },
    )


@app.post("/client-error", status_code=204, tags=["system"], include_in_schema=False)
def client_error(body: ClientErrorIn, request: Request):
    """An unhandled error in a browser. Does nothing at all without a tracker,
    so an open route costs nothing until there is somewhere to send them."""
    if monitoring.endpoint() is None:
        return None
    try:
        ratelimit.hit("client-error", _client_ip(request), limit=20, window_s=3600)
    except ratelimit.RateLimited:
        return None
    monitoring.capture(service="browser", message=body.message,
                       tags={"page": (body.page or "")[:80]})
    return None


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
    # Before the account exists, not after: an account nobody can verify would
    # just sit on the address.
    if not mailer.can_send():
        log.error("sign-up refused: EMAIL_API_KEY is not set")
        raise HTTPException(503, "Sign-up is not available right now because we can't send "
                                 "email. Please try again later.")
    _api_base()                                 # same: refuse before creating anything

    user = auth.register_user(body.email, body.password,
                              full_name=body.name, company=body.company)
    try:
        _send_verification(user)
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
    return RedirectResponse(f"{_site_base()}/signin.html?{outcome}", status_code=303)


@app.post("/auth/resend-verification", status_code=202, tags=["auth"])
def resend_verification(body: ResendIn, request: Request, tasks: BackgroundTasks):
    """Mail a new link. Always the same answer, so it reveals no accounts."""
    email = auth.normalise_email(body.email)
    try:
        ratelimit.hit("resend", _client_ip(request), limit=REGISTER_LIMIT, window_s=3600)
    except ratelimit.RateLimited:
        raise HTTPException(429, "Too many requests from this network. "
                                 "Try again in an hour.") from None
    user = db.get_user_by_email(email)
    if user is not None and not auth.is_verified(user):
        try:
            ratelimit.hit("resend-address", email, limit=EMAIL_PER_ADDRESS, window_s=3600)
            tasks.add_task(_resend_quietly, user)
        except ratelimit.RateLimited:
            pass                 # the same answer: a limit reveals an account too
    return {"status": "sent_if_registered"}


@app.post("/auth/forgot-password", status_code=202, tags=["auth"])
def forgot_password(body: ForgotIn, request: Request, tasks: BackgroundTasks):
    """Mail a reset link. Always the same answer, so it reveals no accounts.

    Works on an unverified account too: that is how an address someone else
    registered gets recovered by the person who actually holds it.
    """
    email = auth.normalise_email(body.email)
    try:
        ratelimit.hit("forgot", _client_ip(request), limit=REGISTER_LIMIT, window_s=3600)
    except ratelimit.RateLimited:
        raise HTTPException(429, "Too many requests from this network. "
                                 "Try again in an hour.") from None
    user = db.get_user_by_email(email)
    if user is not None:
        try:
            ratelimit.hit("forgot-address", email, limit=EMAIL_PER_ADDRESS, window_s=3600)
            tasks.add_task(_send_reset_quietly, user)
        except ratelimit.RateLimited:
            pass
    return {"status": "sent_if_registered"}


@app.post("/auth/reset-password", tags=["auth"])
def reset_password(body: ResetIn):
    """Finish a reset. Every session on the account ends."""
    user = auth.reset_password(body.token, body.password)
    if user is None:
        raise HTTPException(400, "This reset link is invalid or has expired. "
                                 "Ask for a new one.")
    return {"status": "password_changed"}


@app.post("/auth/change-password", response_model=TokenOut, tags=["auth"])
def change_password(body: ChangePasswordIn, user_id: str = Depends(session_user_id)):
    """Change the password while signed in. Every session ends, including this
    one, so a new token comes back for the tab that asked. A wrong current password
    is a 403: a 401 would tell the page the session had died."""
    try:
        auth.change_password(user_id, body.current_password, body.new_password)
    except auth.InvalidCredentials as exc:
        raise HTTPException(403, str(exc)) from None
    user = db.get_user_by_id(user_id)
    return TokenOut(token=auth.create_session(user.id), user_id=user.id,
                    email=user.email, expires_in=auth.SESSION_TTL_SECONDS)


@app.post("/auth/login", response_model=TokenOut, tags=["auth"])
def login(body: LoginIn, request: Request):
    # Failures only, counted in the database because every serverless instance
    # would otherwise keep its own count. Two counters: one address from one
    # network, and everything from one network. Not the address alone, or anyone
    # could lock a stranger out of their account by failing on purpose.
    email = auth.normalise_email(body.email)
    ip = _client_ip(request)
    pair = f"{ip}|{email}"
    if (ratelimit.over("login", pair, LOGIN_LIMIT, LOGIN_WINDOW_S)
            or ratelimit.over("login-ip", ip, LOGIN_LIMIT * 5, LOGIN_WINDOW_S)):
        raise HTTPException(429, "Too many failed attempts. Wait 15 minutes, or reset "
                                 "your password.")
    try:
        user = auth.login_user(email, body.password)
    except auth.InvalidCredentials:
        ratelimit.record("login", pair)
        ratelimit.record("login-ip", ip)
        raise
    auth.require_verified(user)          # 403 "Please verify your email first."
    return TokenOut(token=auth.create_session(user.id), user_id=user.id,
                    email=user.email, expires_in=auth.SESSION_TTL_SECONDS)


@app.post("/auth/logout", status_code=204, tags=["auth"])
def logout(claims: dict = Depends(session_claims)):
    """End this session on the server, not just in the browser that asked."""
    auth.revoke_session(claims)
    return None


@app.get("/auth/me", tags=["auth"])
def me(user_id: str = Depends(current_user_id)):
    user = db.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(401, "The account for this session no longer exists.")
    return {"id": user.id, "email": user.email,
            "name": user.full_name, "company": user.company,
            "created_at": dashboard_data._iso(user.created_at)}


@app.get("/auth/me/export", tags=["auth"])
def export_account(user_id: str = Depends(session_user_id)):
    """Everything stored about this account, as a JSON download."""
    return JSONResponse(account.export(user_id), headers={
        "Content-Disposition": 'attachment; filename="cynea-account-export.json"'})


@app.delete("/auth/me", status_code=204, tags=["auth"])
def delete_account(body: DeleteAccountIn, user_id: str = Depends(session_user_id)):
    """Permanently delete the account, its agents, calls and recordings."""
    if body.confirm != "DELETE":
        raise HTTPException(400, 'Type DELETE to confirm.')
    user = db.get_user_by_id(user_id)
    if not auth.verify_password(body.password, user.password_hash):
        raise HTTPException(403, "That password isn't right, so nothing was deleted.")
    try:
        account.delete_account(user_id)
    except recordings.StorageError as exc:
        log.error("account deletion stopped: %s", exc)
        raise HTTPException(503, "We couldn't remove your recordings, so nothing was "
                                 "deleted. Try again in a moment.") from None
    return None


# ----------------------------------------------------------------------
# API keys
# ----------------------------------------------------------------------

def _key_json(k) -> dict:
    return {"id": k.id, "name": k.name, "prefix": k.prefix,
            "created_at": dashboard_data._iso(k.created_at),
            "last_used_at": dashboard_data._iso(k.last_used_at)}


@app.get("/api-keys", tags=["integrations"])
def list_api_keys(user_id: str = Depends(session_user_id)):
    return [_key_json(k) for k in apikeys.list_for(user_id)]


@app.post("/api-keys", status_code=201, tags=["integrations"])
def create_api_key(body: ApiKeyIn, user_id: str = Depends(session_user_id)):
    """Make a key. The `key` field is the only time it is ever shown."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Give the key a name.")
    if len(apikeys.list_for(user_id)) >= apikeys.MAX_PER_USER:
        raise HTTPException(400, f"An account can have {apikeys.MAX_PER_USER} keys. "
                                 "Revoke one first.")
    row, raw = apikeys.create(user_id, name)
    return {**_key_json(row), "key": raw}


@app.delete("/api-keys/{key_id}", status_code=204, tags=["integrations"])
def revoke_api_key(key_id: str, user_id: str = Depends(session_user_id)):
    if not apikeys.revoke(user_id, key_id):
        raise HTTPException(404, "API key not found.")
    return None


# ----------------------------------------------------------------------
# Webhooks
# ----------------------------------------------------------------------

def _hook_json(h) -> dict:
    return {"id": h.id, "url": h.url, "created_at": dashboard_data._iso(h.created_at),
            "last_status": h.last_status,
            "last_delivery_at": dashboard_data._iso(h.last_delivery_at)}


@app.get("/webhooks", tags=["integrations"])
def list_webhooks(user_id: str = Depends(session_user_id)):
    return [_hook_json(h) for h in webhooks.list_for(user_id)]


@app.post("/webhooks", status_code=201, tags=["integrations"])
def create_webhook(body: WebhookIn, user_id: str = Depends(session_user_id)):
    """Add a receiver. The `secret` field is the only time it is ever shown."""
    if len(webhooks.list_for(user_id)) >= webhooks.MAX_PER_USER:
        raise HTTPException(400, f"An account can have {webhooks.MAX_PER_USER} webhooks. "
                                 "Remove one first.")
    try:
        hook = webhooks.create(user_id, body.url)
    except webhooks.BadWebhookURL as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**_hook_json(hook), "secret": hook.secret}


@app.post("/webhooks/{hook_id}/test", tags=["integrations"])
def test_webhook(hook_id: str, user_id: str = Depends(session_user_id)):
    """Send a test event now and say what the receiver answered."""
    if webhooks.get_owned(user_id, hook_id) is None:
        raise HTTPException(404, "Webhook not found.")
    return {"status": webhooks.deliver(hook_id, "webhook.test", {"message": "This is a test."})}


@app.delete("/webhooks/{hook_id}", status_code=204, tags=["integrations"])
def delete_webhook(hook_id: str, user_id: str = Depends(session_user_id)):
    if not webhooks.remove(user_id, hook_id):
        raise HTTPException(404, "Webhook not found.")
    return None


# ----------------------------------------------------------------------
# Agents
# ----------------------------------------------------------------------

@app.get("/agents/options", tags=["agents"])
def agent_options(user_id: str = Depends(current_user_id)):
    """The personas and voices an agent can be made from."""
    from cynea.agent_loader import _PERSONAS

    return {
        "personas": [{"id": key, "name": dashboard_data._display_name(key),
                      "role": dashboard_data.PERSONA_META.get(key, {}).get("role"),
                      "photo": dashboard_data.PERSONA_META.get(key, {}).get("photo"),
                      "default_voice": (spec.get("voice") or {}).get("voice")}
                     for key, spec in sorted(_PERSONAS.items())],
        "voices": _voice_options(),
    }


@app.post("/agents", status_code=201, tags=["agents"])
def create_agent(body: AgentIn, user_id: str = Depends(current_user_id)):
    from cynea.agent_loader import _PERSONAS

    persona = _known_persona(body.persona)
    voice = _clean_voice_config(body.voice_config or {})
    if not voice.get("voice"):
        spec = _PERSONAS[persona]["voice"]
        voice = {"provider": "edge_tts", "voice": spec.get("voice"),
                 "speed": spec.get("speed", 1.0), **voice}
    agent = db.create_agent(user_id, _clean_name(body.name), persona, voice)
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


@app.post("/agents/{agent_id}/call-ticket", status_code=201, tags=["agents"])
def call_ticket(agent_id: str, user_id: str = Depends(current_user_id)):
    """A short-lived pass for one test call on this agent's saved settings."""
    agent = _own_agent(agent_id, user_id)
    try:
        ticket = call_config.issue_ticket(agent)
    except call_config.AgentPaused:
        raise HTTPException(409, "This agent is paused. Resume it to call it.") from None
    return {"ticket": ticket, "expires_in": call_config.TICKET_TTL_S}


@app.get("/call-config", tags=["agents"], include_in_schema=False)
def get_call_config(ticket: str = Query(..., max_length=2048)):
    """What the call socket trades a ticket for. The ticket is the credential."""
    saved = call_config.config_for_ticket(ticket)
    if saved is None:
        raise HTTPException(403, "This call ticket is not valid.")
    return saved


@app.put("/agents/{agent_id}", tags=["agents"])
def update_agent(agent_id: str, body: AgentUpdate,
                 user_id: str = Depends(current_user_id)):
    agent = _own_agent(agent_id, user_id)
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items()
              if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update.")
    if "name" in fields:
        fields["name"] = _clean_name(fields["name"])
    if "persona" in fields:
        fields["persona"] = _known_persona(fields["persona"])
    # Paused lives in voice_config: it needs no column, and so no migration.
    # A partial update merges rather than wiping what it didn't mention.
    if "voice_config" in fields or "paused" in fields:
        merged = dict(agent.voice_config or {})
        merged.update(_clean_voice_config(fields.pop("voice_config", {})))
        if "paused" in fields:
            merged["paused"] = bool(fields.pop("paused"))
        fields["voice_config"] = merged
    try:
        agent = db.update_agent(agent_id, **fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _agent_json(agent)


@app.delete("/agents/{agent_id}", status_code=204, tags=["agents"])
def delete_agent(agent_id: str, user_id: str = Depends(current_user_id)):
    _own_agent(agent_id, user_id)
    try:
        account.delete_agent(agent_id)
    except recordings.StorageError as exc:
        log.error("agent deletion stopped: %s", exc)
        raise HTTPException(503, "We couldn't remove this agent's recordings, so nothing was "
                                 "deleted. Try again in a moment.") from None
    return None


@app.post("/agents/{agent_id}/prompt", status_code=201, tags=["agents"])
def save_prompt(agent_id: str, body: PromptIn, user_id: str = Depends(current_user_id)):
    """Append a prompt version. This is what the editor's Save button needs."""
    _own_agent(agent_id, user_id)
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "content is required.")
    v = db.save_prompt_version(agent_id, content)
    return {"id": v.id, "version": v.version,
            "created_at": dashboard_data._iso(v.created_at)}


@app.get("/agents/{agent_id}/prompts", tags=["agents"])
def list_prompts(agent_id: str, user_id: str = Depends(current_user_id)):
    _own_agent(agent_id, user_id)
    return [
        {"id": v.id, "version": v.version, "content": v.content,
         "created_at": dashboard_data._iso(v.created_at)}
        for v in db.get_prompt_versions(agent_id)
    ]


# ----------------------------------------------------------------------
# Calls
# ----------------------------------------------------------------------

@app.post("/calls", status_code=201, tags=["calls"])
def log_call(body: CallIn, tasks: BackgroundTasks, user_id: str = Depends(current_user_id)):
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
    # After the response, so a customer's slow receiver cannot slow this down.
    # No transcript or caller number: a receiver can fetch those with a key.
    tasks.add_task(webhooks.fan_out, user_id, "call.logged", {
        "call_id": call.id, "agent_id": call.agent_id, "status": call.status,
        "duration_s": call.duration_s, "cost_cents": call.cost_cents,
        "sentiment_score": call.sentiment_score,
        "created_at": dashboard_data._iso(call.created_at),
    })
    return {"id": call.id, "status": call.status,
            "created_at": dashboard_data._iso(call.created_at)}


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
def get_recording(call_id: str, user_id: str = Depends(current_user_id)):
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
        url = recordings.playback_url(call.audio_url, call.id, _api_base())
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
# Telephony
# ----------------------------------------------------------------------

@app.post("/tel/twilio/voice", tags=["telephony"], include_in_schema=False)
async def twilio_voice_webhook(request: Request):
    """Twilio calls this when a call arrives, and speaks the TwiML it gets back.

    Touches no database: a caller is on the line, and Neon's cold start alone takes
    seconds. The signature is checked before anything in the body is believed.
    """
    form = await request.form()
    try:
        verified = twilio_voice.signature_ok(form, request.headers.get("X-Twilio-Signature", ""))
    except twilio_voice.NotConfigured as exc:
        log.error("twilio webhook refused: %s", exc)
        raise HTTPException(503, "Twilio is not configured on this server.") from None
    if not verified:
        log.warning("twilio webhook refused: bad signature")
        raise HTTPException(403, "Invalid Twilio signature.")
    if not twilio_voice.account_ok(form):
        log.warning("twilio webhook refused: call is on another account")
        raise HTTPException(403, "This call is not on the configured account.")

    # print, not log: Vercel's Python runtime shows stdout, and the root logger
    # drops info. One line per call, whatever the fields contain.
    fields = {k: " ".join(str(form.get(k)).split()) for k in ("CallSid", "From", "To")}
    print(f"[twilio] inbound call CallSid={fields['CallSid']} From={fields['From']} "
          f"To={fields['To']}", flush=True)
    return Response(content=twilio_voice.TWIML, headers={"Content-Type": "text/xml"})


# ----------------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------------

@app.get("/dashboard/stats", tags=["dashboard"])
def dashboard_stats(tz_offset_min: int = Query(0, ge=-840, le=840),
                    user_id: str = Depends(current_user_id)):
    return dashboard_data.get_dashboard_stats(user_id=user_id, tz_offset_min=tz_offset_min)


@app.get("/dashboard/queue", tags=["dashboard"])
def dashboard_queue(user_id: str = Depends(current_user_id)):
    return dashboard_data.get_live_queue(user_id=user_id)


@app.get("/dashboard/bootstrap", tags=["dashboard"])
def dashboard_bootstrap(tz_offset_min: int = Query(0, ge=-840, le=840),
                        user_id: str = Depends(current_user_id)):
    """Everything the console needs on load, in one round trip.

    The dashboard otherwise fires four requests before it can paint; on a
    slow mobile connection that is four round trips of latency, plus a
    possible Neon cold start on each.
    """
    return {
        "stats": dashboard_data.get_dashboard_stats(user_id=user_id, tz_offset_min=tz_offset_min),
        "queue": dashboard_data.get_live_queue(user_id=user_id),
        "calls": dashboard_data.get_call_history(user_id=user_id, limit=25),
    }


if PRODUCTION:
    @app.get("/openapi.json", include_in_schema=False)
    def openapi_schema(user_id: str = Depends(current_user_id)):
        """The schema, for signed-in callers only."""
        return JSONResponse(app.openapi())


__all__ = ["app"]
