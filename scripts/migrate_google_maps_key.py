"""
Add google_maps_api_key column to runtime_config so super_admins can
rotate the Google Maps key from Settings without SSH.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(runtime_config)"))).fetchall()}
        if "google_maps_api_key" not in cols:
            await conn.execute(text("ALTER TABLE runtime_config ADD COLUMN google_maps_api_key TEXT"))
            print("+ added runtime_config.google_maps_api_key")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
