"""Seed the Cynea database with a demo workspace.

    python -m cynea.seed              # create what is missing
    python -m cynea.seed --calls 40   # also generate sample call history
    python -m cynea.seed --reset      # delete the demo user first

Creates one demo user and the four personas as agents, taking each
agent's voice config straight from its persona module so the seed cannot
drift from the prompts of record. The first prompt version is stored too,
which gives the agent editor something real to show.

Idempotent: re-running skips anything already present and reports it.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta, timezone

import cynea  # noqa: F401  — loads .env so DATABASE_URL is visible
from cynea import auth, db
from cynea.agent_loader import _PERSONAS

DEMO_EMAIL = "demo@cynea.ai"
DEMO_PASSWORD = "demo1234"

# name + role shown in the console, keyed by persona module
AGENT_SPECS = [
    ("kwame", "Kwame",  "Hotel Receptionist"),
    ("amina", "Amina",  "Bank Support"),
    ("kofi",  "Kofi",   "Restaurant Orders"),
    ("maya",  "Maya",   "Bookings & Scheduling"),
]

# Realistic-looking sample calls, used only with --calls.
_SAMPLE_CALLS = {
    "kwame": [
        ("Booking confirmed", "resolved", 0.62, 79, 5,
         "Caller asked about a double room for the 14th and 15th. Confirmed "
         "availability, took the booking under Ama Mensah, read the total back."),
        ("Late checkout granted", "resolved", 0.55, 127, 6,
         "Caller asked to keep room 214 until 2pm. Confirmed availability, "
         "granted at no charge, noted against the reservation."),
        ("Caller hung up", "abandoned", -0.12, 38, 2,
         "Caller asked about rates then ended the call before a quote was given."),
    ],
    "amina": [
        ("Escalated to human", "escalated", -0.31, 164, 9,
         "Card declined on a recurring payment. Verified the account holder, "
         "could not determine the decline reason, escalated to payments."),
        ("Balance provided", "resolved", 0.41, 52, 3,
         "Caller asked for a current balance. Verified identity and read it back."),
        ("Escalated to human", "escalated", -0.44, 192, 11,
         "Duplicate charge on a statement. Confirmed two identical charges, "
         "escalated to billing for a refund decision."),
    ],
    "kofi": [
        ("Order placed", "resolved", 0.48, 112, 6,
         "Takeaway order: two jollof plates and one grilled tilapia. Checked "
         "allergies, read the order and total back, confirmed pickup time."),
        ("Order placed", "resolved", 0.57, 96, 5,
         "Delivery order taken and address confirmed twice before payment."),
    ],
    "maya": [
        ("Appointment booked", "resolved", 0.51, 88, 4,
         "Booked a Thursday slot. Confirmed East Africa Time out loud and "
         "sent a confirmation before ending the call."),
        ("Caller hung up", "abandoned", -0.08, 41, 2,
         "Caller asked to move an appointment then hung up during the time "
         "zone check. No change was made."),
    ],
}


def _mask(url: str) -> str:
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.partition("@")
    return f"{scheme}://***:***@{host}"


def seed(with_calls: int = 0, reset: bool = False) -> int:
    try:
        url = db.get_database_url()
    except db.DatabaseNotConfigured as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    print(f"Database: {_mask(url)}")
    if not db.healthcheck():
        print("\nDatabase is unreachable. Run: python -m cynea.migrate --check\n",
              file=sys.stderr)
        return 1
    print("Connected.\n")

    db.init_db()   # safe if the tables already exist

    # ---- user --------------------------------------------------------
    if reset:
        existing = db.get_user_by_email(DEMO_EMAIL)
        if existing:
            with db.session_scope() as s:
                s.delete(s.get(db.User, existing.id))
            print(f"  reset    deleted {DEMO_EMAIL} and everything under it")

    user = db.get_user_by_email(DEMO_EMAIL)
    if user is None:
        user = auth.register_user(DEMO_EMAIL, DEMO_PASSWORD)
        print(f"  created  user     {user.email}  id={user.id}")
        print(f"                    password: {DEMO_PASSWORD}  (demo only)")
    else:
        print(f"  exists   user     {user.email}  id={user.id}")

    # ---- agents ------------------------------------------------------
    existing_agents = {a.persona: a for a in db.get_agents_by_user(user.id)}
    agents = {}

    for persona_key, display_name, role in AGENT_SPECS:
        persona = _PERSONAS.get(persona_key)
        if persona is None:
            print(f"  SKIPPED  agent    {display_name}: no persona module found")
            continue

        if persona_key in existing_agents:
            agent = existing_agents[persona_key]
            print(f"  exists   agent    {agent.name:<22} persona={persona_key:<6} id={agent.id}")
        else:
            # Voice config comes from the persona module, so the seeded
            # agent and the running agent can never disagree.
            agent = db.create_agent(
                user_id=user.id,
                name=f"{display_name} - {role}",
                persona=persona_key,
                voice_config=dict(persona["voice"]),
            )
            print(f"  created  agent    {agent.name:<22} persona={persona_key:<6} id={agent.id}")
            print(f"                    voice: {persona['voice'].get('provider')}"
                  f" / {persona['voice'].get('voice')}")

        agents[persona_key] = agent

        # ---- first prompt version ------------------------------------
        if db.get_latest_prompt(agent.id) is None:
            version = db.save_prompt_version(agent.id, persona["prompt"])
            print(f"           prompt   v{version.version} stored "
                  f"({len(persona['prompt'])} chars)")

    # ---- optional sample call history --------------------------------
    if with_calls:
        print()
        made = _seed_calls(agents, with_calls)
        print(f"  created  calls    {made} sample records")

    # ---- summary -----------------------------------------------------
    print("\nSummary")
    print(f"  user   : {user.email}")
    print(f"  agents : {len(db.get_agents_by_user(user.id))}")
    total_calls = sum(len(db.get_calls_by_agent(a.id, limit=1000))
                      for a in db.get_agents_by_user(user.id))
    print(f"  calls  : {total_calls}")
    print("\nSign in with demo@cynea.ai / demo1234")
    return 0


def _seed_calls(agents: dict, target: int) -> int:
    """Generate call history spread over the last 7 days.

    Uses a fixed seed so re-running produces the same distribution rather
    than a different-looking dashboard every time.
    """
    rng = random.Random(20260825)
    made = 0
    numbers = {
        "kwame": "+233 24 ••• {:04d}",
        "kofi":  "+233 20 ••• {:04d}",
        "amina": "+254 71 ••• {:04d}",
        "maya":  "+254 72 ••• {:04d}",
    }

    while made < target:
        for key, agent in agents.items():
            if made >= target:
                break
            templates = _SAMPLE_CALLS.get(key)
            if not templates:
                continue
            outcome, status, sentiment, duration, cost, transcript = rng.choice(templates)

            jitter = rng.uniform(0.85, 1.15)
            call = db.log_call(
                agent_id=agent.id,
                caller_number=numbers[key].format(rng.randint(1000, 9999)),
                duration=int(duration * jitter),
                transcript=f"{outcome}\n\n{transcript}",
                sentiment=round(max(-1.0, min(1.0, sentiment + rng.uniform(-.06, .06))), 3),
                cost=max(1, int(cost * jitter)),
                status=status,
            )
            # Spread backwards over the last week so "today" is populated
            # but the 7d view has depth. log_call() stamps created_at with
            # now(), so it needs a direct update.
            when = datetime.now(timezone.utc) - timedelta(
                days=rng.randint(0, 6), hours=rng.randint(0, 14),
                minutes=rng.randint(0, 59),
            )
            with db.session_scope() as s:
                row = s.get(db.Call, call.id)
                row.created_at = when
            made += 1
    return made


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Seed the Cynea demo workspace.")
    ap.add_argument("--calls", type=int, default=0, metavar="N",
                    help="also create N sample call records across the agents")
    ap.add_argument("--reset", action="store_true",
                    help="delete the demo user (and its agents/calls) first")
    args = ap.parse_args(argv)
    return seed(with_calls=args.calls, reset=args.reset)


if __name__ == "__main__":
    raise SystemExit(main())
