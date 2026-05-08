"""
Add linkedin_profile_json + linkedin_profile_fetched_at to contacts.
Caches the Netrows /people/profile-by-url payload so we can re-render
without a fresh API call. On-demand refresh via the contact card.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


COLUMNS = [
    ("linkedin_profile_json",       "TEXT"),
    ("linkedin_profile_fetched_at", "DATETIME"),
]


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(contacts)"))).fetchall()}
        for name, ddl in COLUMNS:
            if name not in cols:
                await conn.execute(text(f"ALTER TABLE contacts ADD COLUMN {name} {ddl}"))
                print(f"+ added contacts.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
