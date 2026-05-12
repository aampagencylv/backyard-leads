"""
Native scheduler v3 — booking-page brand customization columns:

  scheduling_configs:
    - brand_color (default '#E65100')
    - accent_bg_color (default '#FFF8F0')
    - logo_url (TEXT, nullable)

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


COLUMNS = [
    ("brand_color",      "TEXT NOT NULL DEFAULT '#E65100'"),
    ("accent_bg_color",  "TEXT NOT NULL DEFAULT '#FFF8F0'"),
    ("logo_url",         "TEXT"),
]


async def main() -> None:
    async with engine.begin() as conn:
        for name, ddl in COLUMNS:
            if not await column_exists(conn, "scheduling_configs", name):
                await conn.execute(text(f"ALTER TABLE scheduling_configs ADD COLUMN {name} {ddl}"))
                print(f"+ added scheduling_configs.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
