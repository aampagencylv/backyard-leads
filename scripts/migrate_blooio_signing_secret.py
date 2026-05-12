"""
Add runtime_config.blooio_signing_secret — used to HMAC-verify inbound
webhooks from Blooio so an attacker who knows the URL can't forge fake
'message.received' events.
Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        if not await column_exists(conn, "runtime_config", "blooio_signing_secret"):
            await conn.execute(text("ALTER TABLE runtime_config ADD COLUMN blooio_signing_secret TEXT"))
            print("+ added runtime_config.blooio_signing_secret")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
