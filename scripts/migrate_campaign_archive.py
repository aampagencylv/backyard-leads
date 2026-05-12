"""
Adds Campaign.archived_at — when set, the campaign is hidden from the
main Auto Pilot list. Used after a campaign completes so the page
stays focused on active work.

Idempotent. Chained in init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


COLUMNS = [
    ("archived_at", "TIMESTAMPTZ"),
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
