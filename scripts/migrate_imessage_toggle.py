"""Per-tenant iMessage send toggle.

Decouples "do we send iMessage" from "is the Blooio API key set." A
tenant can have a Blooio key configured but choose not to send (because
their device link is down, because they want a clean pause, because
they're not ready for an SMS-adjacent channel yet, etc.).

When imessage_enabled is FALSE, the sequence engine marks every
iMessage step as skipped at dispatch time instead of attempting a
send + failing repeatedly. The next step in the sequence is no longer
blocked.

Defaults to FALSE so adding a new tenant doesn't accidentally send
iMessage before the operator confirms the channel is healthy.

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine
from app.services.migration_utils import column_exists


async def main() -> None:
    async with engine.begin() as conn:
        if not await column_exists(conn, "runtime_config", "imessage_enabled"):
            await conn.execute(text(
                "ALTER TABLE runtime_config ADD COLUMN imessage_enabled "
                "BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            print("+ runtime_config.imessage_enabled added (default FALSE)")
    print("Migration complete — iMessage toggle ready.")


if __name__ == "__main__":
    asyncio.run(main())
