#!/usr/bin/env python3
"""Remove the demo workspace that cynea/seed_demo.py provisioned.

    python tools/purge_demo.py            # dry run: shows exactly what would go
    python tools/purge_demo.py --apply    # deletes, in one transaction

What goes: the demo account (demo+demo@cynea.ai). Its four seeded agents,
their twenty `[DEMO]` calls and any prompt versions go with it through the
foreign keys' ON DELETE CASCADE — nothing is deleted by pattern alone.

What stays: every other account, its agents and its calls. The script counts
them before and after and refuses to commit if any of those numbers moved,
or if the demo workspace holds anything it did not expect to find (a call
without the [DEMO] tag means someone used the demo login for real, and that
is a decision for a person, not this script).

Re-running `python -m cynea.seed_demo` brings the demo workspace back.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cynea  # noqa: F401,E402 — loads .env
from cynea import db  # noqa: E402
from cynea.seed_demo import DEMO_EMAIL, DEMO_TAG  # noqa: E402
from sqlalchemy import func, select  # noqa: E402


def _others(s, demo_id) -> dict:
    """Rows that must not change: everything outside the demo account."""
    agents = select(db.Agent.id).where(db.Agent.user_id != demo_id)
    return {
        "users": s.scalar(select(func.count()).select_from(db.User).where(db.User.id != demo_id)),
        "agents": s.scalar(select(func.count()).select_from(db.Agent).where(db.Agent.user_id != demo_id)),
        "calls": s.scalar(select(func.count()).select_from(db.Call).where(db.Call.agent_id.in_(agents))),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="delete (default is a dry run)")
    args = ap.parse_args()

    with db.session_scope() as s:
        user = s.scalar(select(db.User).where(db.User.email == DEMO_EMAIL))
        if user is None:
            print(f"No account {DEMO_EMAIL}; nothing to remove.")
            return 0

        agents = s.scalars(select(db.Agent).where(db.Agent.user_id == user.id)).all()
        ids = [a.id for a in agents]
        calls = s.scalars(select(db.Call).where(db.Call.agent_id.in_(ids))).all() if ids else []
        untagged = [c for c in calls if not (c.transcript or "").startswith(DEMO_TAG)]
        before = _others(s, user.id)

        print(f"demo account   {DEMO_EMAIL}")
        print(f"  agents       {len(agents)}: " + ", ".join(sorted(a.name for a in agents)))
        print(f"  calls        {len(calls)} ({len(calls) - len(untagged)} tagged {DEMO_TAG})")
        if calls:
            days = sorted({c.created_at.date().isoformat() for c in calls if c.created_at})
            print(f"  dated        {days[0]} .. {days[-1]}")
        print(f"everything else (must not change): {before}")

        if untagged:
            print(f"\nREFUSING: {len(untagged)} call(s) in the demo workspace are not tagged "
                  f"{DEMO_TAG}. Someone may have used the demo login for real; check them first.")
            return 2
        if not args.apply:
            print("\nDry run — nothing deleted. Re-run with --apply.")
            return 0

        s.delete(user)
        s.flush()

        after = _others(s, user.id)
        gone = s.scalar(select(func.count()).select_from(db.Agent).where(db.Agent.user_id == user.id))
        if after != before or gone:
            s.rollback()
            print(f"\nROLLED BACK: other rows changed ({before} -> {after}) or demo agents remain ({gone}).")
            # Raised, not returned: session_scope commits on a normal exit,
            # and SystemExit passes its `except Exception` straight to close().
            raise SystemExit(3)

    print(f"\nDeleted the demo account, {len(agents)} agents and {len(calls)} calls. "
          f"Everything else unchanged: {after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
