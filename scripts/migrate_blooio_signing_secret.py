"""
Add runtime_config.blooio_signing_secret — used to HMAC-verify inbound
webhooks from Blooio so an attacker who knows the URL can't forge fake
'message.received' events.
Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(runtime_config)"))).fetchall()}
        if "blooio_signing_secret" not in cols:
            await conn.execute(text("ALTER TABLE runtime_config ADD COLUMN blooio_signing_secret TEXT"))
            print("+ added runtime_config.blooio_signing_secret")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
