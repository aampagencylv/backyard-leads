"""
Add runtime_config.resend_webhook_secret so Steve can rotate the inbound
email webhook signing secret from Settings UI without SSHing into the box.

DB-first read: the inbound webhook verifier checks runtime_config first,
falls back to settings.resend_webhook_secret (env) if empty.
Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        if not await column_exists(conn, "runtime_config", "resend_webhook_secret"):
            await conn.execute(text("ALTER TABLE runtime_config ADD COLUMN resend_webhook_secret TEXT"))
            print("+ added runtime_config.resend_webhook_secret")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
