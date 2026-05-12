"""
Missive sidebar v2 — store Missive conversation_id on Contact so
status-change hooks can fire write-back actions against the right
thread.

  contacts:
    - missive_conversation_id  (TEXT, nullable, indexed)
    - missive_conversation_seen_at  (DATETIME, nullable)

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


COLUMNS = [
    ("missive_conversation_id",       "TEXT"),
    ("missive_conversation_seen_at",  "DATETIME"),
]


async def main() -> None:
    async with engine.begin() as conn:
        added: list[str] = []
        for name, ddl in COLUMNS:
            if not await column_exists(conn, "contacts", name):
                await conn.execute(text(f"ALTER TABLE contacts ADD COLUMN {name} {ddl}"))
                added.append(name)
                print(f"+ added contacts.{name}")
        if "missive_conversation_id" in added:
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_contacts_missive_conversation_id "
                "ON contacts(missive_conversation_id)"
            ))
            print("+ added index on contacts.missive_conversation_id")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
