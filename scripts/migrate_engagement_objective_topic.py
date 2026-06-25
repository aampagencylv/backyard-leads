"""Persist the sequence agenda on the engagement (engagements.objective) and
the per-step topic on each action (actions.topic), so BOTH the enrollment-time
and the send-time content generation can use them. Idempotent."""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine

async def _col(conn, t, c):
    r = await conn.execute(text("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=:t AND column_name=:c)"), {"t": t, "c": c})
    return bool(r.scalar())

async def main():
    async with engine.begin() as conn:
        if not await _col(conn, "engagements", "objective"):
            await conn.execute(text("ALTER TABLE engagements ADD COLUMN objective TEXT"))
            print("+ engagements.objective")
        if not await _col(conn, "actions", "topic"):
            await conn.execute(text("ALTER TABLE actions ADD COLUMN topic TEXT"))
            print("+ actions.topic")
        print("done")

if __name__ == "__main__":
    asyncio.run(main())
