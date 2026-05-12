"""
Add SMS opt-out columns to contacts (TCPA compliance, Twilio Phase 6).
Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


COLUMNS = [
    ("do_not_text",    "BOOLEAN NOT NULL DEFAULT 0"),
    ("do_not_text_at", "DATETIME"),
]


async def main() -> None:
    async with engine.begin() as conn:
        for name, ddl in COLUMNS:
            if not await column_exists(conn, "contacts", name):
                await conn.execute(text(f"ALTER TABLE contacts ADD COLUMN {name} {ddl}"))
                print(f"+ added contacts.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
