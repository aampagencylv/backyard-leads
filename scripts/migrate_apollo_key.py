"""
Add runtime_config.apollo_api_key — the one customer-supplied integration
in the SaaS model. Tenants who already pay Apollo plug their key in to
unlock decision-maker contacts + direct dials for B2B/SaaS verticals.

Tenant-tier (admins manage from Settings UI). No env fallback by design:
if a tenant hasn't entered a key, Apollo paths are simply skipped.

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        if not await column_exists(conn, "runtime_config", "apollo_api_key"):
            await conn.execute(text("ALTER TABLE runtime_config ADD COLUMN apollo_api_key TEXT"))
            print("+ added runtime_config.apollo_api_key")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
