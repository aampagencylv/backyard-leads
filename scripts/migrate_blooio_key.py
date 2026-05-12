"""
Add blooio_api_key column to runtime_config.
Used for iMessage/SMS via Blooio (Twilio Phase 6 — messaging).
Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        if not await column_exists(conn, "runtime_config", "blooio_api_key"):
            await conn.execute(text("ALTER TABLE runtime_config ADD COLUMN blooio_api_key TEXT"))
            print("+ added runtime_config.blooio_api_key")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
