"""Add runtime_config.compliance_address — the per-tenant CAN-SPAM physical
postal address shown in every outbound email footer. Empty by default; each
tenant enters their own real mailing address at onboarding (legally required).
We never fall back to another tenant's address. Nullable TEXT. Idempotent."""
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
        if not await _col(conn, "runtime_config", "compliance_address"):
            await conn.execute(text("ALTER TABLE runtime_config ADD COLUMN compliance_address TEXT"))
            print("+ added runtime_config.compliance_address")
            # Backfill ONLY the BMP tenant (id=1) with the previous global
            # CAN-SPAM address so BMP's live sends never hit the new
            # "no address → refuse" guard during cutover. Every OTHER tenant
            # starts empty and must enter their own real address (the whole
            # point — we never ship one tenant BMP's address).
            try:
                from app.config import settings
                addr = (settings.bmp_postal_address or "").strip()
                if addr:
                    await conn.execute(text(
                        "UPDATE runtime_config SET compliance_address = :a "
                        "WHERE tenant_id = 1 AND (compliance_address IS NULL OR compliance_address = '')"
                    ), {"a": addr})
                    print("  backfilled tenant 1 (BMP) compliance_address")
            except Exception as e:
                print(f"  (backfill skipped: {e})")
        else:
            print("= already present")

if __name__ == "__main__":
    asyncio.run(main())
