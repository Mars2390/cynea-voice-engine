"""Transactional email: the verification link and the password-reset link.

Sent through Resend's HTTP API (free tier: 100 emails/day, 3,000/month),
over httpx like every other provider in this codebase, so there is no SDK.

These links are credentials, so they are never written to a log. A log line
says that a link was generated and for which address, nothing more.

Environment:
    EMAIL_API_KEY       Resend API key (re_...). Without it nothing can be sent,
                        and sign-up refuses rather than pretending it did.
    EMAIL_FROM          Sender, e.g. "Cynea <hello@cynea.ai>". Resend only sends
                        from a domain you have verified with them; until then
                        "onboarding@resend.dev" can only deliver to your own
                        Resend account address.
    EMAIL_DEV_LOG_LINKS "1" prints links to the terminal instead, for working on
                        a laptop with no key. Ignored in production.
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


def _production() -> bool:
    return os.getenv("ENV", "").strip().lower() == "production" or bool(os.getenv("VERCEL"))


def _dev_links() -> bool:
    return os.getenv("EMAIL_DEV_LOG_LINKS", "").strip() == "1" and not _production()


def can_send() -> bool:
    """Would a send reach anyone? Asked before an account is created, so nobody
    is left holding an account whose verification email never existed."""
    return bool(os.getenv("EMAIL_API_KEY", "").strip()) or _dev_links()


def send_verification(to_email: str, link: str, name: str = "") -> str:
    """Send the verification link. Returns "sent" or "logged".

    Raises EmailError if it cannot be delivered: a sign-up that silently never
    emails is worse than one that says so.
    """
    hello = f"Hi {html.escape(name.split()[0])}," if name.strip() else "Hi,"
    return _send(
        to_email, link, "verification",
        subject="Verify your email for Cynea",
        body=(f"<p>{hello}</p>"
              "<p>Confirm your email to finish setting up your Cynea account.</p>"
              f'<p><a href="{html.escape(link, quote=True)}">Verify my email</a></p>'
              "<p>The link expires in 24 hours. If you did not sign up, ignore this email.</p>"),
        text=f"{hello}\n\nVerify your email: {link}\n\nThe link expires in 24 hours.",
    )


def send_password_reset(to_email: str, link: str) -> str:
    """Send a password-reset link. Same delivery and failure rules."""
    return _send(
        to_email, link, "password reset",
        subject="Reset your Cynea password",
        body=("<p>Hi,</p>"
              "<p>Someone asked to reset the password on this Cynea account.</p>"
              f'<p><a href="{html.escape(link, quote=True)}">Choose a new password</a></p>'
              "<p>The link expires in an hour. If this wasn't you, ignore this email: "
              "your password stays as it is.</p>"),
        text=(f"Choose a new Cynea password: {link}\n\nThe link expires in an hour. "
              "If this wasn't you, ignore this email."),
    )


def _send(to_email: str, link: str, kind: str, subject: str, body: str, text: str) -> str:
    key = os.getenv("EMAIL_API_KEY", "").strip()
    log.info("%s link generated for %s", kind, to_email)
    if not key:
        if _dev_links():
            print(f"[cynea] {kind} link for {to_email}: {link}", flush=True)
            return "logged"
        raise EmailError("EMAIL_API_KEY is not set, so no email can be sent.")

    import httpx

    try:
        r = httpx.post(_RESEND, timeout=15.0,
                       headers={"Authorization": f"Bearer {key}"},
                       json={"from": os.getenv("EMAIL_FROM", _DEFAULT_FROM),
                             "to": [to_email],
                             "subject": subject,
                             "html": body,
                             "text": text})
    except Exception as exc:
        raise EmailError(f"email provider unreachable: {exc}") from exc
    if r.status_code >= 400:
        raise EmailError(f"email provider returned {r.status_code}: {r.text[:200]}")
    return "sent"
