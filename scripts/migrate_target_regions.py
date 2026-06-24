"""Add runtime_config.target_regions — the per-tenant list of geographic
markets the prospector targets (e.g. BMP: US metros; AAMP: Costa Rica,
Cancún). Captured at onboarding; pre-fills the Auto Pilot location field.
JSON list of strings, nullable. Idempotent."""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine

async def _col(conn, table, col):
    r = await conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=:t AND column_name=:c)"
    ), {"t": table, "c": col})
    return bool(r.scalar())

async def main():
    async with engine.begin() as conn:
        if not await _col(conn, "runtime_config", "target_regions"):
            await conn.execute(text("ALTER TABLE runtime_config ADD COLUMN target_regions TEXT"))
            print("+ added runtime_config.target_regions")
        else:
            print("= already present")

if __name__ == "__main__":
    asyncio.run(main())
