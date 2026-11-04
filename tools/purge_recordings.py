#!/usr/bin/env python3
"""Delete call recordings older than the retention period.

    python tools/purge_recordings.py             # delete what is past retention
    python tools/purge_recordings.py --dry-run   # list it, delete nothing
    python tools/purge_recordings.py --days 7    # override RECORDING_RETENTION_DAYS

Retention is RECORDING_RETENTION_DAYS (default 30). For each expired
recording the stored object (R2 or local disk) is deleted first, then its
row, and the call's audio_url is cleared — so a call never points at audio
that is gone. A recording whose object cannot be deleted is left in place
and reported, rather than orphaned in storage with no row to find it by.

Run it on a schedule: a Vercel Cron hitting a protected endpoint, a
GitHub Action, or cron on any box with DATABASE_URL and the R2_* variables.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cynea  # noqa: F401,E402 — loads .env
from cynea import db, recordings  # noqa: E402
from sqlalchemy import select  # noqa: E402


def purge(days: int, dry_run: bool = False) -> dict:
    cutoff = db._now() - timedelta(days=days)
    done = {"expired": 0, "deleted": 0, "failed": 0, "bytes": 0}
    with db.session_scope() as s:
        expired = s.scalars(select(db.CallRecording)
                            .where(db.CallRecording.created_at < cutoff)).all()
        done["expired"] = len(expired)
        for rec in expired:
            call = s.get(db.Call, rec.call_id)
            url = (call.audio_url if call and call.audio_url
                   and call.audio_url.endswith(rec.storage_key) else None)
            print(f"  {'would delete' if dry_run else 'deleting'} {rec.storage_key} "
                  f"({rec.bytes} bytes, {rec.created_at:%Y-%m-%d})")
            if dry_run:
                continue
            try:
                if url:
                    recordings.delete(url)
                elif recordings.backend() == "local":
                    recordings.delete(f"local://{rec.storage_key}")
            except recordings.StorageError as exc:
                print(f"    kept: {exc}", file=sys.stderr)
                done["failed"] += 1
                continue
            if call and url:
                call.audio_url = None
            done["bytes"] += rec.bytes or 0
            s.delete(rec)
            done["deleted"] += 1
    return done


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=recordings.retention_days())
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    print(f"retention: {args.days} days, storage: {recordings.backend()}")
    r = purge(args.days, args.dry_run)
    print(f"{r['expired']} expired, {r['deleted']} deleted ({r['bytes']} bytes), "
          f"{r['failed']} failed")
    return 1 if r["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
