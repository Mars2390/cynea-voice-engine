"""Create the Cynea database schema.

    python -m cynea.migrate            # create any missing tables
    python -m cynea.migrate --check    # report status, change nothing
    python -m cynea.migrate --drop     # DESTRUCTIVE: drop then recreate

Safe to run repeatedly: `create_all` only creates what is missing, so this
doubles as the "is my DATABASE_URL right?" check.
"""

from __future__ import annotations

import argparse
import sys

import cynea  # noqa: F401  — loads .env so DATABASE_URL is visible
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Create the Cynea schema.")
    ap.add_argument("--check", action="store_true",
                    help="report connectivity and existing tables, change nothing")
    ap.add_argument("--drop", action="store_true",
                    help="DROP every Cynea table, then recreate. Destroys data.")
    args = ap.parse_args(argv)

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
    print(f"Schema ready: {sorted(now & wanted)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
