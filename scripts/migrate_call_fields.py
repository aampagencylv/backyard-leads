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


async def _index_exists(conn, name: str) -> bool:
    """Cross-dialect index existence check."""
    dialect = conn.engine.url.get_backend_name() if hasattr(conn, "engine") else conn.dialect.name
    if dialect == "sqlite":
        row = (await conn.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n"
        ), {"n": name})).first()
        return row is not None
    row = (await conn.execute(text(
        "SELECT 1 FROM pg_indexes WHERE indexname=:n"
    ), {"n": name})).first()
    return row is not None


async def main() -> None:
    async with engine.begin() as conn:
        for name, ddl in COLUMNS:
            if not await column_exists(conn, "activities", name):
                await conn.execute(text(f"ALTER TABLE activities ADD COLUMN {name} {ddl}"))
                print(f"+ added activities.{name}")

        # Index on twilio_call_sid for webhook lookups
        if not await _index_exists(conn, "ix_activities_twilio_call_sid"):
            await conn.execute(text(
                "CREATE INDEX ix_activities_twilio_call_sid ON activities(twilio_call_sid)"
            ))
            print("+ created index ix_activities_twilio_call_sid")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
