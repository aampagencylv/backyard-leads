"""Add Resend domain tracking to runtime_config.

When a new tenant is created, we provision a Resend sending subdomain
(go.{slug}.leadprospector.ai). The Resend API returns:
  - domain_id (UUID)
  - DNS records (SPF / DKIM / DMARC) that need to land in DNS

We store both on the tenant's RuntimeConfig:
  - resend_domain_id           — the Resend identifier (for later API calls)
  - resend_domain_name         — the actual hostname (e.g. go.acme.leadprospector.ai)
  - resend_domain_records_json — the records blob, displayed in /admin so the
                                 platform admin can copy them to DNS
  - resend_domain_status       — last known status (not_started/pending/verified/failed)

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine
from app.services.migration_utils import column_exists


async def main() -> None:
    async with engine.begin() as conn:
        for name, ddl in (
            ("resend_domain_id",          "ALTER TABLE runtime_config ADD COLUMN resend_domain_id VARCHAR(64)"),
            ("resend_domain_name",        "ALTER TABLE runtime_config ADD COLUMN resend_domain_name VARCHAR(255)"),
            ("resend_domain_records_json","ALTER TABLE runtime_config ADD COLUMN resend_domain_records_json TEXT"),
            ("resend_domain_status",      "ALTER TABLE runtime_config ADD COLUMN resend_domain_status VARCHAR(32)"),
        ):
            if not await column_exists(conn, "runtime_config", name):
                await conn.execute(text(ddl))
                print(f"+ runtime_config.{name}")

    print("Migration complete — Resend domain tracking ready.")


if __name__ == "__main__":
    asyncio.run(main())
