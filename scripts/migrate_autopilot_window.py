"""
Autopilot send-window config — adds three columns to runtime_config so
admins can constrain when the sequence engine fires email/iMessage/SMS.

Defaults: 8am-7pm (19:00) in the contact's local timezone, all 7 days.
Steve's stated preference — "don't send at midnight" but no weekend
restriction yet.

  runtime_config:
    - autopilot_send_start_hour (INTEGER NOT NULL DEFAULT 8)
    - autopilot_send_end_hour   (INTEGER NOT NULL DEFAULT 19)
    - autopilot_send_days_json  (TEXT, nullable — null = every day)

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


COLUMNS = [
    ("autopilot_send_start_hour", "INTEGER NOT NULL DEFAULT 8"),
    ("autopilot_send_end_hour",   "INTEGER NOT NULL DEFAULT 19"),
    ("autopilot_send_days_json",  "TEXT"),
]


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(runtime_config)"))).fetchall()}
        for name, ddl in COLUMNS:
            if name not in cols:
                await conn.execute(text(f"ALTER TABLE runtime_config ADD COLUMN {name} {ddl}"))
                print(f"+ added runtime_config.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
