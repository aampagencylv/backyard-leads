"""Add snooze fields to deals. Idempotent."""
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


async def migrate():
    async with engine.begin() as conn:
        cols = await conn.execute(text("PRAGMA table_info(deals)"))
        col_names = [r[1] for r in cols.fetchall()]
        for col, coltype in [
            ("snoozed_until", "DATETIME"),
            ("snooze_reason", "TEXT"),
            ("stage_before_snooze", "VARCHAR(50)"),
        ]:
            if col not in col_names:
                await conn.execute(text(f"ALTER TABLE deals ADD COLUMN {col} {coltype}"))
                print(f"migrate_snooze: added deals.{col}")


if __name__ == "__main__":
    asyncio.run(migrate())
