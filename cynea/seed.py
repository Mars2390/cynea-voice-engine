"""Provision the four Cynea agents for a real account.

    python -m cynea.seed --email you@cynea.ai --password '<your password>'

Creates the four personas as agents owned by that account, each with its
real prompt stored as version 1. Nothing else: no demo login, no sample
calls, no invented metrics. Every figure the console shows afterwards is
one the system actually produced.

If the account does not exist it is created with the password you supply,
so this doubles as first-run provisioning. If it does exist, you must pass
its real password — the script will not attach agents to an account it
cannot authenticate.

Why an account is required
--------------------------
`agents.user_id` is NOT NULL: an agent belongs to a workspace, and that is
what keeps one customer's agents invisible to another. There is no such
thing as an ownerless agent, so provisioning agents means naming who owns
them.

Re-running is safe: personas already present are reported and skipped.
"""

from __future__ import annotations

import argparse
import getpass
import sys

import cynea  # noqa: F401  — loads .env so DATABASE_URL is visible
from cynea import auth, db
from cynea.agent_loader import _PERSONAS

# The four shipped personas, with the role label the console displays.
AGENT_SPECS = [
    ("kwame", "Kwame",  "Hotel Receptionist"),
    ("amina", "Amina",  "Bank Support"),
    ("kofi",  "Kofi",   "Restaurant Orders"),
    ("maya",  "Maya",   "Bookings & Scheduling"),
]


def _mask(url: str) -> str:
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.partition("@")
    return f"{scheme}://***:***@{host}"


def _resolve_owner(email: str, password: str):
    """Return the account that will own the agents, creating it if new."""
    existing = db.get_user_by_email(email)

    if existing is None:
        user = auth.register_user(email, password)
        print(f"  created  account  {user.email}")
        return user

    # Never attach agents to an account we cannot prove ownership of.
    try:
        user = auth.login_user(email, password)
    except auth.InvalidCredentials:
        print(
            f"\n{email} already exists but that password is wrong.\n"
            f"Pass the account's real password, or use a different --email.\n",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"  exists   account  {user.email}")
    return user


def seed(email: str, password: str) -> int:
    try:
        url = db.get_database_url()
    except db.DatabaseNotConfigured as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    print(f"Database: {_mask(url)}")
    if not db.healthcheck():
        print("\nDatabase unreachable. Check with: python -m cynea.migrate --check\n",
              file=sys.stderr)
        return 1
    print("Connected.\n")

    db.init_db()
    user = _resolve_owner(email, password)

    existing = {a.persona: a for a in db.get_agents_by_user(user.id)}
    created = 0

    for persona_key, display_name, role in AGENT_SPECS:
        persona = _PERSONAS.get(persona_key)
        if persona is None:
            print(f"  SKIPPED  agent    {display_name}: no persona module found")
            continue

        if persona_key in existing:
            print(f"  exists   agent    {display_name:<6} ({persona_key})")
            agent = existing[persona_key]
        else:
            # Voice config is read from the persona module, so a seeded
            # agent and a running one can never disagree about the voice.
            agent = db.create_agent(
                user_id=user.id,
                name=f"{display_name} - {role}",
                persona=persona_key,
                voice_config=dict(persona["voice"]),
            )
            created += 1
            print(f"  created  agent    {display_name:<6} ({persona_key})  "
                  f"{persona['voice'].get('provider')}/{persona['voice'].get('voice')}")

        if db.get_latest_prompt(agent.id) is None:
            version = db.save_prompt_version(agent.id, persona["prompt"])
            print(f"           prompt   v{version.version}  "
                  f"{len(persona['prompt']):,} chars from "
                  f"cynea_africa/persona/{persona_key}.py")

    agents = db.get_agents_by_user(user.id)
    calls = sum(len(db.get_calls_by_agent(a.id, limit=1)) for a in agents)

    print(f"\n  account : {user.email}")
    print(f"  agents  : {len(agents)}  ({created} created this run)")
    print(f"  calls   : {calls}  (populated by real calls only)")
    print(f"\nSign in at signin.html with {user.email}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Provision the four Cynea agents for a real account.",
    )
    ap.add_argument("--email", required=True,
                    help="account that will own the agents")
    ap.add_argument("--password", default=None,
                    help="account password; prompted securely if omitted")
    args = ap.parse_args(argv)

    password = args.password
    if not password:
        # Prompting keeps the password out of shell history.
        password = getpass.getpass(f"Password for {args.email}: ")
    if not password:
        print("A password is required.", file=sys.stderr)
        return 2

    try:
        return seed(args.email, password)
    except auth.WeakPassword as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2
    except auth.AuthError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
