"""Report unhandled errors to Sentry, over plain HTTP.

No SDK: it would add a dependency to the API and does not run in a Python
Worker at all, while the protocol is one POST of three lines of JSON. Sentry's
free plan is enough, and any service that accepts Sentry envelopes works.

    SENTRY_DSN   https://<public key>@<host>/<project id>. Unset means off.

What is sent is the exception type, its message, the stack and a few tags.
Never request bodies, headers or transcripts: those hold callers' words and
credentials, and an error tracker is the wrong place for either.
"""

from __future__ import annotations

import json
import logging
import os
import re
import traceback
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple
from urllib.parse import urlparse

log = logging.getLogger("cynea.monitoring")

_TIMEOUT_S = 3.0
_MAX_MESSAGE = 1000

# Anything shaped like a credential is cut from a message before it leaves.
_SECRETS = re.compile(r"(bearer\s+\S+|cynea_[\w-]+|whsec_[\w-]+|gsk_\w+|csk-\w+|re_\w{8,}"
                      r"|postgres(?:ql)?://\S+|token=\S+|sig=\S+)", re.IGNORECASE)


def scrub(text: str) -> str:
    return _SECRETS.sub("[removed]", str(text))[:_MAX_MESSAGE]


def endpoint(dsn: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """(envelope URL, X-Sentry-Auth header) for a DSN, or None if there isn't one."""
    dsn = (dsn if dsn is not None else os.getenv("SENTRY_DSN", "")).strip()
    if not dsn:
        return None
    parts = urlparse(dsn)
    project = parts.path.strip("/").rsplit("/", 1)[-1]
    if not parts.username or not parts.hostname or not project:
        log.error("SENTRY_DSN is not a valid DSN; errors are not being reported")
        return None
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return (f"{parts.scheme}://{host}/api/{project}/envelope/",
            f"Sentry sentry_version=7, sentry_key={parts.username}, sentry_client=cynea/1.0")


def _frames(exc: BaseException) -> list:
    frames = []
    for f in traceback.extract_tb(exc.__traceback__):
        name = f.filename.replace("\\", "/")
        # Paths from the package down: the rest is only where it was installed.
        for root in ("/cynea/", "/cynea_africa/", "/src/", "/site-packages/"):
            if root in name:
                name = root.lstrip("/") + name.split(root, 1)[1]
                break
        frames.append({"filename": name, "function": f.name, "lineno": f.lineno,
                       "in_app": "site-packages" not in name})
    return frames


def envelope(exc: Optional[BaseException] = None, service: str = "api",
             message: Optional[str] = None, tags: Optional[dict] = None) -> bytes:
    """One event as a Sentry envelope. Pass an exception, or a message for an
    error that arrived as text (a browser's)."""
    now = datetime.now(timezone.utc).isoformat()
    event_id = uuid.uuid4().hex
    event = {
        "event_id": event_id, "timestamp": now, "platform": "python", "level": "error",
        "server_name": service,
        "environment": os.getenv("ENV", "").strip().lower() or "development",
        "tags": {"service": service, **(tags or {})},
    }
    if exc is not None:
        event["exception"] = {"values": [{
            "type": type(exc).__name__, "value": scrub(exc),
            "stacktrace": {"frames": _frames(exc)},
        }]}
    else:
        event["message"] = {"formatted": scrub(message or "unknown error")}
    head = json.dumps({"event_id": event_id, "sent_at": now})
    item = json.dumps({"type": "event", "content_type": "application/json"})
    return "\n".join((head, item, json.dumps(event))).encode("utf-8")


def capture(exc: Optional[BaseException] = None, service: str = "api",
            message: Optional[str] = None, tags: Optional[dict] = None) -> bool:
    """Send one event. True if it was accepted. Never raises: reporting a failure
    must not become a second failure."""
    target = endpoint()
    if target is None:
        return False
    url, auth = target
    try:
        import httpx

        r = httpx.post(url, content=envelope(exc, service, message, tags), timeout=_TIMEOUT_S,
                       headers={"X-Sentry-Auth": auth,
                                "Content-Type": "application/x-sentry-envelope"})
        return r.status_code < 300
    except Exception as err:
        log.warning("could not report an error: %s", type(err).__name__)
        return False
