"""Phase B-3b: tenant_domains.is_verified + verified_at.

The verify endpoint flips is_verified to TRUE on success. Caddy's
on-demand TLS ask endpoint requires is_verified=TRUE before allowing
cert issuance, which prevents an attacker from CNAMEing a domain to
us, registering it via a compromised tenant admin, and triggering
Let's Encrypt rate limits.

BMP's pre-existing seed domains are flipped to verified — they're
known good, no DNS proof needed.

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine
from app.services.migration_utils import column_exists


async def main() -> None:
    async with engine.begin() as conn:
        if not await column_exists(conn, "tenant_domains", "is_verified"):
            await conn.execute(text(
                "ALTER TABLE tenant_domains ADD COLUMN is_verified BOOLEAN "
                "NOT NULL DEFAULT FALSE"
            ))
            print("+ tenant_domains.is_verified added")
        if not await column_exists(conn, "tenant_domains", "verified_at"):
            await conn.execute(text(
                "ALTER TABLE tenant_domains ADD COLUMN verified_at TIMESTAMPTZ"
            ))
            print("+ tenant_domains.verified_at added")

        # Trust BMP's seed domains.
        await conn.execute(text(
            "UPDATE tenant_domains SET is_verified = TRUE, verified_at = NOW() "
            "WHERE tenant_id = 1 AND is_verified = FALSE"
        ))
    print("Migration complete — tenant_domains.is_verified ready.")


if __name__ == "__main__":
    asyncio.run(main())
