"""
Add runtime_config.messaging_direction — org-wide messaging tone/direction
prepended to every AI generation system prompt. Lets the team steer the voice
and the strategic angle without code changes.
Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(runtime_config)"))).fetchall()}
        if "messaging_direction" not in cols:
            await conn.execute(text("ALTER TABLE runtime_config ADD COLUMN messaging_direction TEXT"))
            print("+ added runtime_config.messaging_direction")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
