"""
Add per-rep dial preferences for Phase 4 of Twilio Voice.

  users.dial_mode             — 'browser' (default) or 'bridge'
  users.personal_phone_number — E.164; required when dial_mode='bridge'

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


COLUMNS = [
    ("dial_mode",             "VARCHAR(20) NOT NULL DEFAULT 'browser'"),
    ("personal_phone_number", "VARCHAR(40)"),
]


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(users)"))).fetchall()}
        for name, ddl in COLUMNS:
            if name not in cols:
                await conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {ddl}"))
                print(f"+ added users.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
