"""
Add phone-type cache columns to contacts. Populated lazily by Twilio Lookup v2
on the first iMessage send attempt — lets us refuse to send to landlines and
skip the lookup cost on every subsequent send.
Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


COLUMNS = [
    ("phone_type",            "VARCHAR(20)"),
    ("phone_type_checked_at", "DATETIME"),
    ("phone_carrier",         "VARCHAR(80)"),
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
