"""Create the Cynea database schema.

    python -m cynea.migrate                          # create missing tables and columns
    python -m cynea.migrate --check                  # report status, change nothing
    python -m cynea.migrate --make-admin EMAIL       # migrate, then make EMAIL an admin
    python -m cynea.migrate --drop                   # DESTRUCTIVE: drop then recreate

Safe to run repeatedly: `create_all` only creates what is missing and
`upgrade_schema` only adds missing nullable columns, so this doubles as the
"is my DATABASE_URL right?" check.

Account approval (users.status, approved_at, approved_by, is_admin)
-------------------------------------------------------------------
Adding those columns approves every existing account that verified its email
and leaves unverified ones pending. Nobody is an admin until --make-admin names
them: the address is an argument rather than something written in this file, so
no account is special in the code.
"""

from __future__ import annotations

import argparse
import sys

import cynea  # noqa: F401  — loads .env so DATABASE_URL is visible
from cynea import db
from cynea.db import (
    Base,
    DatabaseNotConfigured,
    get_database_url,
    get_engine,
    healthcheck,
    init_db,
)


def _mask(url: str) -> str:
    """Never print credentials, even into a private terminal."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.partition("@")
    return f"{scheme}://***:***@{host}"


def _accounts_summary(engine) -> str:
    """Accounts by approval status and how many admins, once the columns exist."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("users"):
        return "no users table"
    cols = {c["name"] for c in insp.get_columns("users")}
    with engine.connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
        if "status" not in cols:
            return f"{total} account(s); no approval columns yet"
        rows = conn.execute(text("SELECT COALESCE(status, 'null'), COUNT(*) FROM users "
                                 "GROUP BY COALESCE(status, 'null')")).all()
        admins = (conn.execute(text("SELECT COUNT(*) FROM users WHERE is_admin = :yes"),
                               {"yes": True}).scalar() if "is_admin" in cols else 0)
    by_status = ", ".join(f"{s} {n}" for s, n in sorted(rows)) or "none"
    return f"{total} account(s): {by_status}; admins {admins}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Create the Cynea schema.")
    ap.add_argument("--check", action="store_true",
                    help="report connectivity and existing tables, change nothing")
    ap.add_argument("--drop", action="store_true",
                    help="DROP every Cynea table, then recreate. Destroys data.")
    ap.add_argument("--make-admin", metavar="EMAIL", action="append", default=[],
                    help="after migrating, give this existing account the admin flag "
                         "(repeat for more than one)")
    args = ap.parse_args(argv)
    if args.check and (args.make_admin or args.drop):
        print("--check changes nothing; run --make-admin or --drop without it.",
              file=sys.stderr)
        return 2

    try:
        url = get_database_url()
    except DatabaseNotConfigured as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    print(f"Database: {_mask(url)}")

    try:
        engine = get_engine()
        from sqlalchemy import inspect
        existing = set(inspect(engine).get_table_names())
    except Exception as exc:
        print(f"\nCould not connect: {exc}\n"
              f"Check the host is reachable and the credentials are current.\n",
              file=sys.stderr)
        return 1

    wanted = set(Base.metadata.tables)
    print(f"Reachable: {healthcheck()}")
    print(f"Existing tables : {sorted(existing & wanted) or 'none'}")
    print(f"Missing tables  : {sorted(wanted - existing) or 'none'}")
    insp = inspect(engine)
    missing_cols = sorted(
        f"{t.name}.{c.name}"
        for t in Base.metadata.sorted_tables if t.name in existing
        for c in t.columns if c.name not in {x["name"] for x in insp.get_columns(t.name)})
    print(f"Missing columns : {missing_cols or 'none'}")
    print(f"Accounts        : {_accounts_summary(engine)}")

    if args.check:
        return 0

    if args.drop:
        confirm = input("\nThis DESTROYS all Cynea data. Type 'drop' to confirm: ")
        if confirm.strip().lower() != "drop":
            print("Aborted; nothing was changed.")
            return 1
        Base.metadata.drop_all(engine)
        print("Dropped.")

    try:
        init_db()
    except DatabaseNotConfigured as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    from sqlalchemy import inspect as _inspect
    now = set(_inspect(engine).get_table_names())
    created = sorted((now & wanted) - existing) if not args.drop else sorted(wanted)
    print(f"\nCreated: {created or 'nothing (already up to date)'}")
    if missing_cols and not args.drop:
        print(f"Columns added: {missing_cols}")
    print(f"Schema ready: {sorted(now & wanted)}")

    failed = False
    for email in args.make_admin:
        if db.make_admin(email) is None:
            print(f"No account has the address {email!r}; nobody was made admin for it.",
                  file=sys.stderr)
            failed = True
        else:
            print(f"Admin: {email.strip().lower()}")
    print(f"Accounts: {_accounts_summary(engine)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
