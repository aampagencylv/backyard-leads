"""Phase B: tenant onboarding state tracker.

Adds `onboarding_step` to tenants — a short string the wizard UI advances
through ('pending' → 'brand' → 'phone' → 'email' → 'a2p' → 'team' →
'plan' → 'done').

Backfills BMP to 'done' since they're a pre-existing live tenant.

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine
from app.services.migration_utils import column_exists


async def main() -> None:
    async with engine.begin() as conn:
        if not await column_exists(conn, "tenants", "onboarding_step"):
            await conn.execute(text(
                "ALTER TABLE tenants ADD COLUMN onboarding_step VARCHAR(32) "
                "NOT NULL DEFAULT 'pending'"
            ))
            # Existing tenants (BMP) are fully onboarded already.
            await conn.execute(text(
                "UPDATE tenants SET onboarding_step = 'done' WHERE id = 1"
            ))
            print("+ tenants.onboarding_step added; BMP set to 'done'")
    print("Migration complete — tenant onboarding state ready.")


if __name__ == "__main__":
    asyncio.run(main())
