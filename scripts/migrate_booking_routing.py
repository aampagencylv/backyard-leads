"""
BDR booking routing + multi-calendar conflict check.

  users:
    - default_booking_host_id (INTEGER, nullable, FK→users.id)
        When set, BDR's audit / signature / sidebar booking links
        route to this user's calendar instead of their own.

  scheduling_configs:
    - conflict_calendar_ids_json (TEXT, nullable)
        JSON array of Google calendar IDs to UNION into free-busy
        when generating slots. NULL = no extra calendars.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


USER_COLUMNS = [
    ("default_booking_host_id", "INTEGER REFERENCES users(id)"),
]
SCHED_COLUMNS = [
    ("conflict_calendar_ids_json", "TEXT"),
]


async def main() -> None:
    async with engine.begin() as conn:
        for name, ddl in USER_COLUMNS:
            if not await column_exists(conn, "users", name):
                await conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {ddl}"))
                print(f"+ added users.{name}")
        for name, ddl in SCHED_COLUMNS:
            if not await column_exists(conn, "scheduling_configs", name):
                await conn.execute(text(f"ALTER TABLE scheduling_configs ADD COLUMN {name} {ddl}"))
                print(f"+ added scheduling_configs.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
