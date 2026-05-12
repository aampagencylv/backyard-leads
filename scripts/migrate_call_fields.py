"""
Add call-related columns to the activities table for Twilio Voice integration.

Idempotent: safe to run on every restart.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


COLUMNS = [
    ("twilio_call_sid",       "VARCHAR(50)"),
    ("call_duration_seconds", "INTEGER"),
    ("call_direction",        "VARCHAR(20)"),
    ("call_outcome",          "VARCHAR(40)"),
    ("recording_url",         "VARCHAR(500)"),
    ("transcript",            "TEXT"),
    ("call_summary",          "TEXT"),
]


async def _columns(conn, table: str) -> set[str]:
    rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
    return {row[1] for row in rows}


async def _indexes(conn, table: str) -> set[str]:
    rows = (await conn.execute(text(f"PRAGMA index_list({table})"))).fetchall()
    return {row[1] for row in rows}


async def main() -> None:
    async with engine.begin() as conn:
        cols = await _columns(conn, "activities")
        for name, ddl in COLUMNS:
            if not await column_exists(conn, "{table}", name):
                await conn.execute(text(f"ALTER TABLE activities ADD COLUMN {name} {ddl}"))
                print(f"+ added activities.{name}")

        # Index on twilio_call_sid for webhook lookups
        idx = await _indexes(conn, "activities")
        if "ix_activities_twilio_call_sid" not in idx:
            await conn.execute(text(
                "CREATE INDEX ix_activities_twilio_call_sid ON activities(twilio_call_sid)"
            ))
            print("+ created index ix_activities_twilio_call_sid")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
