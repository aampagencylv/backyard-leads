"""
Autopilot v2 — per-channel windows + basis radio + presence gate.

  runtime_config:
    - autopilot_basis (TEXT NOT NULL DEFAULT 'contact')
    - autopilot_email_start_hour   (INTEGER NOT NULL DEFAULT 8)
    - autopilot_email_end_hour     (INTEGER NOT NULL DEFAULT 19)
    - autopilot_email_days_json    (TEXT, nullable)
    - autopilot_imessage_start_hour (INTEGER NOT NULL DEFAULT 8)
    - autopilot_imessage_end_hour   (INTEGER NOT NULL DEFAULT 17)
    - autopilot_imessage_days_json  (TEXT, nullable)
    - autopilot_respect_rep_presence (INTEGER NOT NULL DEFAULT 0)

Backfill: when the new columns are added, we seed email's start/end
from the old org-wide autopilot_send_start/end if those exist and
differ from defaults, so a tenant that already set "8am-7pm everyone"
keeps that for email + gets the iMessage default.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


COLUMNS = [
    ("autopilot_basis",                  "TEXT NOT NULL DEFAULT 'contact'"),
    ("autopilot_email_start_hour",       "INTEGER NOT NULL DEFAULT 8"),
    ("autopilot_email_end_hour",         "INTEGER NOT NULL DEFAULT 19"),
    ("autopilot_email_days_json",        "TEXT"),
    ("autopilot_imessage_start_hour",    "INTEGER NOT NULL DEFAULT 8"),
    ("autopilot_imessage_end_hour",      "INTEGER NOT NULL DEFAULT 17"),
    ("autopilot_imessage_days_json",     "TEXT"),
    ("autopilot_respect_rep_presence",   "INTEGER NOT NULL DEFAULT 0"),
]


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(runtime_config)"))).fetchall()}
        added: list[str] = []
        for name, ddl in COLUMNS:
            if name not in cols:
                await conn.execute(text(f"ALTER TABLE runtime_config ADD COLUMN {name} {ddl}"))
                added.append(name)
                print(f"+ added runtime_config.{name}")

        # Backfill email hours from legacy autopilot_send_* fields when
        # we just added the new email columns and a row exists.
        if "autopilot_email_start_hour" in added:
            try:
                await conn.execute(text(
                    "UPDATE runtime_config SET "
                    "  autopilot_email_start_hour = COALESCE(autopilot_send_start_hour, 8), "
                    "  autopilot_email_end_hour   = COALESCE(autopilot_send_end_hour, 19), "
                    "  autopilot_email_days_json  = autopilot_send_days_json "
                    "WHERE id = 1"
                ))
            except Exception as e:
                print(f"  (backfill skipped: {e})")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
