"""
Add daily morning-brief columns to users table.

  brief_enabled         — toggle the brief on/off per user (default on)
  brief_hour            — local-time hour (0-23) the brief should arrive
  timezone              — IANA tz name, used to convert brief_hour to UTC
                          for the cron loop check
  last_brief_sent_at    — idempotency stamp so the cron doesn't double-send
                          if it ticks twice during the same hour window

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


COLUMNS = [
    ("brief_enabled",       "BOOLEAN NOT NULL DEFAULT 1"),
    ("brief_hour",          "INTEGER NOT NULL DEFAULT 7"),
    ("timezone",            "VARCHAR(80) NOT NULL DEFAULT 'America/Phoenix'"),
    ("last_brief_sent_at",  "DATETIME"),
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
