"""Add runtime_config.target_business_types — the per-tenant list of
business categories the prospector/Auto Pilot targets.

BMP targets home-service contractors; AAMP targets tour operators /
things-to-do vendors (boat tours, escape rooms, kayak rentals, …). This
was a hardcoded BMP_BUSINESS_TYPES array in the frontend; it's now
per-tenant config. JSON list of strings, nullable (NULL → frontend falls
back to a generic starter set).

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def _column_exists(conn, table: str, col: str) -> bool:
    r = await conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=:t AND column_name=:c)"
    ), {"t": table, "c": col})
    return bool(r.scalar())


async def main() -> None:
    async with engine.begin() as conn:
        if not await _column_exists(conn, "runtime_config", "target_business_types"):
            await conn.execute(text(
                "ALTER TABLE runtime_config ADD COLUMN target_business_types TEXT"
            ))
            print("+ added runtime_config.target_business_types")
        else:
            print("= runtime_config.target_business_types already present")


if __name__ == "__main__":
    asyncio.run(main())
