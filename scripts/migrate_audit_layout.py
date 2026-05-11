"""
Audit-report layout v2:

  scheduling_configs / runtime_config:
    - audit_left_image_url (TEXT, nullable)
    - audit_left_message   (TEXT, nullable)
    - audit_right_image_url (TEXT, nullable)
    - audit_right_message  (TEXT, nullable)
    - audit_scheduler_type (TEXT NOT NULL DEFAULT 'iclosed')
    - audit_native_user_id (INTEGER, nullable)
    - audit_custom_url     (TEXT, nullable)

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


COLUMNS = [
    ("audit_left_image_url",   "TEXT"),
    ("audit_left_message",     "TEXT"),
    ("audit_right_image_url",  "TEXT"),
    ("audit_right_message",    "TEXT"),
    ("audit_scheduler_type",   "TEXT NOT NULL DEFAULT 'iclosed'"),
    ("audit_native_user_id",   "INTEGER"),
    ("audit_custom_url",       "TEXT"),
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
