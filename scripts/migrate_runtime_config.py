"""
Create runtime_config table (single-row org settings).
Idempotent: only creates if missing.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        rows = (await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='runtime_config'"
        ))).fetchall()
        if not rows:
            await conn.execute(text("""
                CREATE TABLE runtime_config (
                    id              INTEGER PRIMARY KEY,
                    netrows_api_key TEXT,
                    updated_at      DATETIME DEFAULT (datetime('now'))
                )
            """))
            await conn.execute(text("INSERT INTO runtime_config (id) VALUES (1)"))
            print("+ created runtime_config table + seed row")
        else:
            print("runtime_config already exists")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
