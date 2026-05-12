"""
One-shot data migration from the SQLite backup to the live Postgres
database. Run once during the platform cutover; not part of the
startup chain.

Reads from /opt/backyard-leads/leads.db.backup-* (a copy made at the
moment we wiped the lead data; users + runtime_config +
scheduling_configs were preserved at the model level but live in the
SQLite file, not Postgres). Inserts those rows into Postgres via the
ORM so type coercion (bool, datetime, JSON) happens automatically.

Tables migrated (operator data only — NOT prospect/lead data):
  - users                  → preserves logins, role, Twilio + Google state
  - runtime_config         → preserves every API key + brand + pipeline + autopilot
  - scheduling_configs     → preserves per-user calendar availability rules
  - credit_ledger          → preserves cost-tracking history
  - audit_log              → preserves admin-action history

Lead data is INTENTIONALLY skipped — companies/contacts/deals/etc. were
wiped by Steve before the cutover so tomorrow starts fresh.
"""
from __future__ import annotations
import asyncio
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import text, inspect as sa_inspect, Boolean, DateTime

from app.database import async_session
from app.models import (
    User, RuntimeConfig, SchedulingConfig, CreditLedger, AuditLogEntry,
)


SQLITE_BACKUP = Path("/opt/backyard-leads/leads.db.backup-20260511-215840")


# (sqlite_table_name, model_class) — order = parents-first for FK safety
MIGRATIONS = [
    ("users", User),
    ("runtime_config", RuntimeConfig),
    ("scheduling_configs", SchedulingConfig),
    ("credit_ledger", CreditLedger),
    ("audit_log", AuditLogEntry),
]


def _parse_dt(s):
    """SQLite stored datetimes as strings. Parse + return naive datetime
    (Postgres columns are TIMESTAMP WITHOUT TIME ZONE in our model)."""
    if s is None or isinstance(s, datetime):
        return s.replace(tzinfo=None) if isinstance(s, datetime) and s.tzinfo else s
    if not isinstance(s, str):
        return s
    s = s.replace("T", " ").split("+")[0].split("Z")[0].strip()
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in s else "%Y-%m-%d %H:%M:%S"
    try:
        return datetime.strptime(s[:26], fmt)
    except (ValueError, TypeError):
        return None


def _coerce_for_model(row: dict, model_class) -> dict:
    """Build a kwargs dict that matches the model's columns, coercing
    SQLite int→bool and string→datetime as needed."""
    out = {}
    mapper = sa_inspect(model_class)
    for col in mapper.columns:
        name = col.name
        if name not in row:
            continue
        val = row[name]
        if val is None:
            out[name] = None
            continue
        if isinstance(col.type, Boolean):
            out[name] = bool(val)
        elif isinstance(col.type, DateTime):
            parsed = _parse_dt(val)
            # Strip tzinfo for TIMESTAMP WITHOUT TIME ZONE columns
            if isinstance(parsed, datetime) and parsed.tzinfo:
                parsed = parsed.replace(tzinfo=None)
            out[name] = parsed
        else:
            out[name] = val
    return out


async def migrate_table(model_class, table: str, src: sqlite3.Connection) -> int:
    """Read all rows from SQLite, INSERT them into Postgres preserving
    primary keys via the ORM. After inserting, advance the Postgres
    sequence past max(id) so SERIAL columns don't collide later."""
    src.row_factory = sqlite3.Row
    rows = [dict(r) for r in src.execute(f"SELECT * FROM {table}").fetchall()]
    if not rows:
        print(f"  {table}: 0 rows — skipping")
        return 0

    async with async_session() as db:
        for raw in rows:
            kwargs = _coerce_for_model(raw, model_class)
            obj = model_class(**kwargs)
            db.add(obj)
        await db.flush()
        # Advance the SERIAL sequence past max(id) so future inserts
        # via the ORM don't collide.
        try:
            await db.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"(SELECT COALESCE(MAX(id), 0) FROM {table}) + 1, false)"
            ))
        except Exception as e:
            print(f"    (sequence reset skipped: {e})")
        await db.commit()
    print(f"  {table}: {len(rows)} rows migrated")
    return len(rows)


async def main() -> None:
    if not SQLITE_BACKUP.exists():
        print(f"Backup not found: {SQLITE_BACKUP}")
        sys.exit(1)
    print(f"Reading from: {SQLITE_BACKUP}")
    print(f"Backup size: {SQLITE_BACKUP.stat().st_size:,} bytes\n")

    src = sqlite3.connect(str(SQLITE_BACKUP))
    total = 0
    for tbl, model in MIGRATIONS:
        try:
            n = await migrate_table(model, tbl, src)
            total += n
        except Exception as e:
            print(f"  ! {tbl} failed: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    src.close()
    print(f"\nDone — {total} total rows migrated.")


if __name__ == "__main__":
    asyncio.run(main())
