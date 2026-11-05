"""What a test call on a customer's agent should use: their saved settings.

The socket runs on Cloudflare and the settings live in Postgres, and the Worker
has no database driver or session secret. So the console asks this API for a
short-lived signed ticket naming one agent, the browser hands that ticket to
the socket, and the socket trades it back here for the agent's settings:

    console  POST /agents/{id}/call-ticket    (signed in; agent must be yours)
    browser  wss://.../voice?ticket=...
    Worker   GET  /call-config?ticket=...     (the ticket is the credential)

A ticket grants one thing: a call on that agent, for five minutes. It can't be
presented as a session, and it can't read anything else.
"""

from __future__ import annotations

from typing import Optional

from cynea import auth, dashboard_data, db
from cynea.voice_demo import DEFAULT_PERSONA, customer_prompt, fill_client

TICKET_PURPOSE = "call"
TICKET_TTL_S = 300


class AgentPaused(Exception):
    """The owner has paused this agent, so it takes no calls."""


def is_paused(agent) -> bool:
    return bool((agent.voice_config or {}).get("paused"))


def issue_ticket(agent) -> str:
    if is_paused(agent):
        raise AgentPaused()
    return auth.sign_value(TICKET_PURPOSE, {"agent": str(agent.id), "user": str(agent.user_id)},
                           TICKET_TTL_S)


def config_for_agent(agent, user) -> dict:
    """The settings a call runs on, as JSON that can cross to the Worker.

    The saved prompt if there is one, else the persona's. Either way it goes
    through customer_prompt(), so the rule about not claiming to be human is
    still the last thing the model reads, whatever the customer typed.
    """
    from cynea.agent_loader import _PERSONAS

    spec = _PERSONAS.get(agent.persona) or _PERSONAS.get(DEFAULT_PERSONA) or {}
    saved = agent.voice_config or {}
    latest = db.get_latest_prompt(agent.id)
    client = (user.company or "").strip() or agent.name
    voice = dashboard_data.agent_voice(agent)
    greeting = saved.get("first_message") or spec.get("first_message", "")
    return {
        "agent_id": str(agent.id),
        "name": agent.name,
        "persona": agent.persona,
        "role": dashboard_data.PERSONA_META.get(agent.persona, {}).get("role"),
        "system_prompt": customer_prompt(latest.content if latest else spec.get("prompt", ""),
                                         client),
        "first_message": fill_client(greeting, client),
        "voice": voice["voice"],
        "speed": voice["speed"],
    }


def config_for_ticket(ticket: str) -> Optional[dict]:
    """The settings a ticket entitles its holder to, or None if it entitles them
    to nothing: forged, expired, the agent deleted or paused since it was issued."""
    data = auth.read_value(TICKET_PURPOSE, ticket)
    if not data:
        return None
    agent = db.get_agent_by_id(data.get("agent"))
    if agent is None or str(agent.user_id) != data.get("user") or is_paused(agent):
        return None
    user = db.get_user_by_id(agent.user_id)
    return config_for_agent(agent, user) if user is not None else None
