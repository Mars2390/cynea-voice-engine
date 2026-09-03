"""Provision a DEMO workspace: one account, four agents, twenty calls.

    python -m cynea.seed_demo
    python -m cynea.seed_demo --email demo@cynea.ai --password 'something'
    python -m cynea.seed_demo --reset          # delete the demo agents first

This exists so the console has a shape to render. An empty dashboard
demonstrates nothing: every tile reads zero, every chart is a flat line,
and a viewer cannot tell a working product from a broken one.

How this differs from `cynea.seed`, deliberately
------------------------------------------------
`cynea.seed` provisions the four agents for a *real* account and writes no
calls at all, on the principle that every figure the console shows should
be one the system actually produced. That principle is right, and this
module breaks it on purpose — so it marks everything it writes.

The demo account's email carries a `+demo` tag, every generated call is
prefixed `[DEMO]` in its transcript, and the caller numbers come from the
ranges reserved for fiction (Ofcom's 07700 900xxx block and the equivalent
+254 7xx 000xxx range) rather than numbers that could ring a real person.
Anyone reading the database can tell in one query which rows are invented:

    SELECT * FROM calls WHERE transcript LIKE '[DEMO]%';

**Do not run this against a customer workspace.** It refuses to attach to
an account whose email does not carry the demo tag unless you pass
--force, because seeding invented calls into a real workspace corrupts the
only numbers anyone is going to trust.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta, timezone

import cynea  # noqa: F401  — loads .env
from cynea import auth, db

DEMO_EMAIL = "demo+demo@cynea.ai"
DEMO_PASSWORD = "cynea-demo-2026"
DEMO_TAG = "[DEMO]"

# Four agents, one per persona, named the way a customer would name them.
AGENTS = [
    ("Adinkra Hotel — Front Desk", "kwame"),
    ("Cynea Bank — Card Support", "amina"),
    ("Asaase Restaurant — Orders", "kofi"),
    ("Cynea — Bookings", "maya"),
]

# Numbers from ranges reserved for drama and documentation, so nothing here
# can dial a real handset if someone copies a row out of the dashboard.
CALLERS = [
    "+254 700 000 118", "+254 700 000 241", "+254 700 000 355",
    "+233 20 000 0164", "+233 20 000 0287", "+234 700 000 0912",
    "+27 60 000 0143", "+256 700 000 221", "+255 700 000 367",
    "+250 700 000 118",
]

# (status, transcript summary, duration range, sentiment range)
OUTCOMES = [
    ("resolved",  "Booked two nights, executive suite, breakfast included.",   (95, 260),  (0.45, 0.9)),
    ("resolved",  "Checked balance and confirmed the last transaction.",       (48, 140),  (0.2, 0.75)),
    ("resolved",  "Took a delivery order and quoted thirty minutes.",          (60, 165),  (0.3, 0.85)),
    ("resolved",  "Moved an appointment to Tuesday and sent an SMS.",          (55, 150),  (0.35, 0.8)),
    ("resolved",  "Answered a rates question and offered a callback.",         (40, 110),  (0.1, 0.6)),
    ("escalated", "Transfer still processing — escalated to the M-Pesa team.", (150, 330), (-0.6, -0.1)),
    ("escalated", "Repeat caller, third attempt — handed to a person.",        (180, 420), (-0.75, -0.2)),
    ("abandoned", "Caller hung up before the agent finished the greeting.",    (4, 14),    (None, None)),
]

# Rough per-second cost of the free stack: transcription and speech cost
# nothing, so this is telephony only. Kept low on purpose — an invented
# figure that flatters the unit economics is worse than no figure.
CENTS_PER_SECOND = 0.15


def _resolve_owner(email: str, password: str, force: bool):
    """Find or create the demo account, refusing real-looking ones."""
    if "+demo@" not in email and not force:
        raise SystemExit(
            f"{email!r} does not look like a demo account.\n"
            "This script writes invented calls, which must never land in a\n"
            "real workspace. Use an address containing '+demo@', or pass\n"
            "--force if you are certain."
        )

    existing = db.get_user_by_email(email)
    if existing:
        if not auth.verify_password(password, existing.password_hash):
            raise SystemExit(
                f"{email} exists but that password is wrong. Pass the real "
                "one with --password, or use --email for a fresh account."
            )
        print(f"  account   {email} (existing)")
        return existing

    user = db.create_user(email, auth.hash_password(password))
    print(f"  account   {email} (created, password: {password})")
    return user


def _clear_demo(user_id: str) -> None:
    """Remove this workspace's agents, and their calls with them.

    Deletion cascades from agents to calls and prompt versions, so this is
    one statement per agent rather than a three-table cleanup.
    """
    agents = db.get_agents_by_user(user_id)
    for a in agents:
        db.delete_agent(str(a.id))
    print(f"  reset     removed {len(agents)} agent(s) and their calls")


def seed(email: str, password: str, calls: int, force: bool, reset: bool) -> int:
    if not db.healthcheck(retries=2, backoff_s=1.5):
        raise SystemExit(
            "The database is not reachable. Check DATABASE_URL in .env, and "
            "that `python -m cynea.migrate` has run."
        )

    user = _resolve_owner(email, password, force)
    user_id = str(user.id)

    if reset:
        _clear_demo(user_id)

    from cynea.agent_loader import _PERSONAS

    have = {a.name: a for a in db.get_agents_by_user(user_id)}
    made = []
    for name, persona in AGENTS:
        if name in have:
            print(f"  agent     {name} (already there)")
            made.append(have[name])
            continue
        if persona not in _PERSONAS:
            print(f"  agent     {name} SKIPPED — persona {persona!r} did not load")
            continue
        spec = _PERSONAS[persona]
        agent = db.create_agent(user_id, name, persona, dict(spec["voice"]))
        db.save_prompt_version(str(agent.id), spec["prompt"])
        print(f"  agent     {name} ({persona})")
        made.append(agent)

    if not made:
        raise SystemExit("No agents available to attach calls to.")

    # A fixed seed so re-running produces the same demo rather than a
    # different one every time somebody looks at it.
    rng = random.Random(20260903)
    now = datetime.now(timezone.utc)
    written = 0

    for i in range(calls):
        agent = made[i % len(made)]
        status, summary, (lo, hi), (s_lo, s_hi) = OUTCOMES[rng.randrange(len(OUTCOMES))]
        duration = rng.randint(lo, hi)
        sentiment = None if s_lo is None else round(rng.uniform(s_lo, s_hi), 2)
        cost = int(duration * CENTS_PER_SECOND)

        call = db.log_call(
            agent_id=str(agent.id),
            caller_number=CALLERS[rng.randrange(len(CALLERS))],
            duration=duration,
            transcript=f"{DEMO_TAG} {summary}",
            sentiment=sentiment,
            cost=cost,
            status=status,
        )

        # Spread the calls over the last three days, weighted toward today
        # so "resolved today" is not zero the moment somebody opens it.
        age_h = rng.choice([rng.uniform(0, 9), rng.uniform(0, 9),
                            rng.uniform(10, 30), rng.uniform(30, 72)])
        _backdate(str(call.id), now - timedelta(hours=age_h))
        written += 1

    print(f"  calls     {written} written, spread over the last 3 days")
    print(f"\nDone. Sign in at the console with {email} / {password}")
    print(f"Remove them again with:  python -m cynea.seed_demo --reset")
    return 0


def _backdate(call_id: str, when: datetime) -> None:
    """Move a call's timestamp.

    `created_at` has a server default, so the row has to be written first
    and adjusted after — there is no argument on log_call for it, and
    adding one would put a demo concern into the production write path.
    """
    with db.session_scope() as s:
        row = s.get(db.Call, call_id)
        if row is not None:
            row.created_at = when


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", default=DEMO_EMAIL)
    ap.add_argument("--password", default=DEMO_PASSWORD)
    ap.add_argument("--calls", type=int, default=20)
    ap.add_argument("--reset", action="store_true",
                    help="delete this workspace's agents and calls first")
    ap.add_argument("--force", action="store_true",
                    help="allow an email without the +demo tag")
    args = ap.parse_args(argv)

    print(f"Seeding demo workspace into {db.get_database_url().split('@')[-1][:40]}…")
    return seed(args.email, args.password, args.calls, args.force, args.reset)


if __name__ == "__main__":
    sys.exit(main())
