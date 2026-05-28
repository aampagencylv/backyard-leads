"""
Adds Campaign.scheduled_start_at — when set on a campaign with
status='scheduled', the activation loop flips it to 'running' once
this UTC time passes. Lets the team schedule autopilot campaigns to
begin on a future date instead of starting immediately.

Idempotent. Chained in init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


COLUMNS = [
    ("scheduled_start_at", "TIMESTAMPTZ"),
]


async def main() -> None:
    async with engine.begin() as conn:
        for name, ddl in COLUMNS:
            if not await column_exists(conn, "campaigns", name):
                await conn.execute(text(f"ALTER TABLE campaigns ADD COLUMN {name} {ddl}"))
                print(f"+ added campaigns.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
