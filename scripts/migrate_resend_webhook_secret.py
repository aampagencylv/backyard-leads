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
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(runtime_config)"))).fetchall()}
        if "resend_webhook_secret" not in cols:
            await conn.execute(text("ALTER TABLE runtime_config ADD COLUMN resend_webhook_secret TEXT"))
            print("+ added runtime_config.resend_webhook_secret")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
