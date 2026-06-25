"""Add sequence_templates.objective — the human-written agenda/goal of the
sequence (e.g. "keep the customer aware of who we are over 120 days"). Fed to
the AI when generating each step's email so the whole sequence stays on-message.
Idempotent."""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine

async def _col(conn, table, col):
    r = await conn.execute(text("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=:t AND column_name=:c)"), {"t": table, "c": col})
    return bool(r.scalar())

async def main():
    async with engine.begin() as conn:
        if not await _col(conn, "sequence_templates", "objective"):
            await conn.execute(text("ALTER TABLE sequence_templates ADD COLUMN objective TEXT"))
            print("+ added sequence_templates.objective")
        else:
            print("= already present")

if __name__ == "__main__":
    asyncio.run(main())
