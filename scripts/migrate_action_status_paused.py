"""Extend actions.status check constraint to allow 'paused'.

The lifecycle module's pause_engagement() needs a non-dispatchable
status for actions whose engagement has been paused by a BDR (deal
won/lost, prospect replied, snooze). 'skipped' isn't right because
that's terminal; we want to be able to flip the action back to
'scheduled' on resume.

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("""
            ALTER TABLE actions DROP CONSTRAINT IF EXISTS actions_status_check
        """))
        await conn.execute(text("""
            ALTER TABLE actions ADD CONSTRAINT actions_status_check
            CHECK (status IN (
                'scheduled', 'sent', 'failed', 'skipped',
                'completed', 'blocked', 'awaiting_approval',
                'paused'
            ))
        """))
        print("+ actions.status now allows 'paused'")


if __name__ == "__main__":
    asyncio.run(main())
