"""Outbound webhooks: tell a customer's server when a call is logged.

Each delivery is a POST of JSON, signed so the receiver can check it came from
here and has not been replayed:

    Cynea-Signature: t=1793863173,v1=<hex hmac_sha256(secret, f"{t}.{body}")>
    Cynea-Event: call.logged
    Cynea-Delivery: <uuid>

The URL is supplied by a user and fetched by this server, which is the shape of
a server-side request forgery. So only public addresses are allowed, checked
when a hook is saved and again before every delivery (a name can be re-pointed
later), and redirects are not followed. What remains is a name that changes
between that check and the request itself: this narrows the window, it does not
close it.

One attempt per event, with a short timeout. There is no retry queue; the
outcome is recorded on the hook, where the console shows it.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import time
import uuid
from typing import List, Optional
from urllib.parse import urlparse

from sqlalchemy import select

from cynea import db

log = logging.getLogger("cynea.webhooks")

MAX_PER_USER = 5
TIMEOUT_S = 5.0
EVENTS = ("call.logged", "webhook.test")


class BadWebhookURL(ValueError):
    """Not a URL this server is willing to send to."""


def _private_allowed() -> bool:
    """For a receiver on a laptop. Never in production."""
    return (os.getenv("WEBHOOK_ALLOW_PRIVATE", "").strip() == "1"
            and os.getenv("ENV", "").strip().lower() != "production"
            and not os.getenv("VERCEL"))


def _address_is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    mapped = getattr(addr, "ipv4_mapped", None)
    return (mapped or addr).is_global


def check_url(url: str) -> str:
    """The URL if it may be used, else BadWebhookURL saying why."""
    url = (url or "").strip()
    try:
        parts = urlparse(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise BadWebhookURL("That isn't a valid URL.") from None
    if len(url) > 2048 or not host:
        raise BadWebhookURL("That isn't a valid URL.")
    if parts.username or parts.password:
        raise BadWebhookURL("A webhook URL can't contain a username or password.")

    allow_private = _private_allowed()
    if parts.scheme != "https" and not (allow_private and parts.scheme == "http"):
        raise BadWebhookURL("A webhook URL has to start with https://.")
    try:
        found = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        raise BadWebhookURL(f"{host} doesn't resolve to anything.") from None
    if not allow_private:
        for *_, sockaddr in found:
            if not _address_is_public(sockaddr[0]):
                raise BadWebhookURL("A webhook has to point at a public address.")
    return url


def create(user_id: str, url: str) -> db.Webhook:
    with db.session_scope() as s:
        hook = db.Webhook(user_id=str(user_id), url=check_url(url),
                          secret="whsec_" + secrets.token_urlsafe(32))
        s.add(hook)
        s.flush()
        return hook


def list_for(user_id: str) -> List[db.Webhook]:
    if not db.table_ready("webhooks"):
        return []
    with db.session_scope() as s:
        return list(s.scalars(select(db.Webhook).where(db.Webhook.user_id == str(user_id))
                              .order_by(db.Webhook.created_at)))


def get_owned(user_id: str, hook_id: str) -> Optional[db.Webhook]:
    if not db._is_uuid(hook_id):
        return None
    with db.session_scope() as s:
        hook = s.get(db.Webhook, str(hook_id))
        return hook if hook is not None and str(hook.user_id) == str(user_id) else None


def remove(user_id: str, hook_id: str) -> bool:
    if get_owned(user_id, hook_id) is None:
        return False
    with db.session_scope() as s:
        s.delete(s.get(db.Webhook, str(hook_id)))
    return True


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("ascii") + body,
                   hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def deliver(hook_id: str, event: str, data: dict) -> int:
    """Send one event to one hook. Returns the status, 0 for no answer. Never
    raises: a customer's broken server must not break the call being logged."""
    import httpx

    with db.session_scope() as s:
        hook = s.get(db.Webhook, str(hook_id))
        if hook is None:
            return 0
        url, secret = hook.url, hook.secret

    delivery = str(uuid.uuid4())
    body = json.dumps({"id": delivery, "type": event, "created": int(time.time()),
                       "data": data}, separators=(",", ":")).encode("utf-8")
    status = 0
    try:
        check_url(url)
        r = httpx.post(url, content=body, timeout=TIMEOUT_S, follow_redirects=False, headers={
            "Content-Type": "application/json",
            "User-Agent": "Cynea-Webhooks/1.0",
            "Cynea-Event": event,
            "Cynea-Delivery": delivery,
            "Cynea-Signature": sign(secret, int(time.time()), body),
        })
        status = r.status_code
    except BadWebhookURL as exc:
        log.warning("webhook %s not sent: %s", hook_id, exc)
    except Exception as exc:
        log.warning("webhook %s delivery failed: %s", hook_id, type(exc).__name__)

    with db.session_scope() as s:
        hook = s.get(db.Webhook, str(hook_id))
        if hook is not None:
            hook.last_status = status
            hook.last_delivery_at = db._now()
    return status


def fan_out(user_id: str, event: str, data: dict) -> None:
    """Every hook this user has. Meant to run after the response has gone."""
    for hook in list_for(user_id):
        deliver(hook.id, event, data)
