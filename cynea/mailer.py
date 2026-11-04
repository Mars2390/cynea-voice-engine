"""Transactional email — today, only the sign-up verification link.

Sent through Resend's HTTP API (free tier: 100 emails/day, 3,000/month),
over httpx like every other provider in this codebase, so there is no SDK.

Environment:
    EMAIL_API_KEY   Resend API key (re_...). Without it nothing is sent: the
                    link is logged instead and the caller is told it went,
                    so sign-up can be exercised end to end on a laptop.
    EMAIL_FROM      Sender, e.g. "Cynea <hello@cynea.ai>". Resend only sends
                    from a domain you have verified with them; until then
                    "onboarding@resend.dev" can only deliver to your own
                    Resend account address.
"""

from __future__ import annotations

import html
import logging
import os

log = logging.getLogger("cynea.mailer")

_RESEND = "https://api.resend.com/emails"
_DEFAULT_FROM = "Cynea <onboarding@resend.dev>"


class EmailError(RuntimeError):
    """The provider refused or could not be reached."""


def send_verification(to_email: str, link: str, name: str = "") -> str:
    """Send the verification link. Returns "sent" or "logged".

    Raises EmailError when a key is configured and the provider fails —
    a sign-up that silently never emails is worse than one that says so.
    """
    key = os.getenv("EMAIL_API_KEY", "").strip()
    if not key:
        # Deliberately loud: this is the only place the link exists.
        log.warning("EMAIL_API_KEY not set — verification link for %s: %s", to_email, link)
        print(f"[cynea] verification link for {to_email}: {link}", flush=True)
        return "logged"

    hello = f"Hi {html.escape(name.split()[0])}," if name.strip() else "Hi,"
    body = (
        f"<p>{hello}</p>"
        "<p>Confirm your email to finish setting up your Cynea account.</p>"
        f'<p><a href="{html.escape(link, quote=True)}">Verify my email</a></p>'
        "<p>The link expires in 24 hours. If you did not sign up, ignore this email.</p>"
    )
    import httpx

    try:
        r = httpx.post(_RESEND, timeout=15.0,
                       headers={"Authorization": f"Bearer {key}"},
                       json={"from": os.getenv("EMAIL_FROM", _DEFAULT_FROM),
                             "to": [to_email],
                             "subject": "Verify your email for Cynea",
                             "html": body,
                             "text": f"{hello}\n\nVerify your email: {link}\n\n"
                                     "The link expires in 24 hours."})
    except Exception as exc:
        raise EmailError(f"email provider unreachable: {exc}") from exc
    if r.status_code >= 400:
        raise EmailError(f"email provider returned {r.status_code}: {r.text[:200]}")
    return "sent"
