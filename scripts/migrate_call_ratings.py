"""Add call rating fields to activities. Idempotent."""
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


async def migrate():
    async with engine.begin() as conn:
        cols = await conn.execute(text("PRAGMA table_info(activities)"))
        col_names = [r[1] for r in cols.fetchall()]

        for col, coltype in [
            ("call_rating", "INTEGER"),
            ("call_feedback", "TEXT"),
            ("rated_by", "INTEGER"),
            ("rated_at", "DATETIME"),
        ]:
            if col not in col_names:
                await conn.execute(text(f"ALTER TABLE activities ADD COLUMN {col} {coltype}"))
                print(f"migrate_call_ratings: added activities.{col}")


if __name__ == "__main__":
    asyncio.run(migrate())
