"""
Add deepgram_api_key column to runtime_config.
Used for telephony-grade transcription of recorded calls.
Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        if not await column_exists(conn, "runtime_config", "deepgram_api_key"):
            await conn.execute(text("ALTER TABLE runtime_config ADD COLUMN deepgram_api_key TEXT"))
            print("+ added runtime_config.deepgram_api_key")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
