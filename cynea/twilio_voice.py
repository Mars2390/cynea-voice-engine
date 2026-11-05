"""Inbound phone calls from Twilio: a fixed test message on a trial account.

Twilio POSTs here when a call arrives, and speaks whatever TwiML comes back.
There is no audio stream: a trial account blocks <Stream>, so nothing reaches the
engine yet. This proves the number, the webhook and the signature check work.

The signature is an HMAC over the URL Twilio was told to call, plus the form
fields, keyed with the account's auth token. So the URL has to be the one pasted
into the Twilio Console, character for character. It is configuration here and
is never rebuilt from the request: behind Vercel's rewrite and the /api mount the
request does not reliably say what Twilio called, and its Host header is whatever
the sender typed.

Environment:
    TWILIO_AUTH_TOKEN   Signs the requests. Without it every call is refused,
                        because an empty key would let anyone forge a signature.
    TWILIO_ACCOUNT_SID  The account calls must belong to.
    TWILIO_VOICE_URL    Only if the URL in the Console is not the default below.
"""

from __future__ import annotations

import os

DEFAULT_VOICE_URL = "https://cynea-voice-engine.vercel.app/api/tel/twilio/voice"

# Static on purpose: nothing from the caller goes into this document. It says what
# it is, as every Cynea agent does, and runs under five seconds spoken.
TWIML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<Response>"
    "<Say>Hello, this is Cynea's AI agent.</Say>"
    '<Pause length="1"/>'
    "<Say>The line works.</Say>"
    "</Response>"
)


class NotConfigured(RuntimeError):
    """No auth token, so no request can be checked."""


def voice_url() -> str:
    """The exact URL pasted into the Twilio Console."""
    return os.getenv("TWILIO_VOICE_URL", "").strip() or DEFAULT_VOICE_URL


def signature_ok(form, signature: str) -> bool:
    """Did Twilio really send this? `form` is the parsed POST body, all of it:
    the signature covers every field, including ones this route never reads."""
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not token:
        raise NotConfigured("TWILIO_AUTH_TOKEN is not set")
    if not signature:
        return False
    # Here rather than at module level, so the rest of the API doesn't load it.
    from twilio.request_validator import RequestValidator

    return RequestValidator(token).validate(voice_url(), form, signature)


def account_ok(form) -> bool:
    """Is this call on our account? Unset means there is nothing to compare."""
    expected = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    return not expected or form.get("AccountSid", expected) == expected


def is_configured() -> bool:
    return bool(os.getenv("TWILIO_AUTH_TOKEN", "").strip())
