"""Phase A: tenant_domains table for custom-domain → tenant routing.

Stores the host header → tenant_id mapping for white-labeled / custom
domains. Subdomain routing ({slug}.agencyprospector.com) is handled in
code via the tenants.slug column and does NOT need a row here.

Seeds the existing BMP hosts as tenant #1 so the resolver immediately
recognizes them once the middleware ships.

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


BMP_HOSTS = [
    ("prospector.backyardmarketingpros.com", True),   # primary
    ("audit.backyardmarketingpros.com", False),
    ("schedule.backyardmarketingpros.com", False),
]


async def _table_exists(conn, name: str) -> bool:
    r = await conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=:n)"
    ), {"n": name})
    return bool(r.scalar())


async def main() -> None:
    async with engine.begin() as conn:
        if not await _table_exists(conn, "tenant_domains"):
            await conn.execute(text("""
                CREATE TABLE tenant_domains (
                    id          SERIAL PRIMARY KEY,
                    tenant_id   INTEGER     NOT NULL REFERENCES tenants(id),
                    domain      VARCHAR(255) NOT NULL UNIQUE,
                    is_primary  BOOLEAN     NOT NULL DEFAULT FALSE,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_tenant_domains_domain "
                "ON tenant_domains(domain)"
            ))
            print("+ created table tenant_domains")
        else:
            # Repair: if the table was already created by Base.metadata.create_all
            # before we added server_default, retrofit the column defaults so
            # future raw-SQL INSERTs (like the seed below) work.
            await conn.execute(text(
                "ALTER TABLE tenant_domains "
                "ALTER COLUMN created_at SET DEFAULT NOW()"
            ))
            await conn.execute(text(
                "ALTER TABLE tenant_domains "
                "ALTER COLUMN is_primary SET DEFAULT FALSE"
            ))

        # Seed BMP's domains as tenant #1. INSERT explicitly sets created_at
        # so we don't depend on the column default being in place.
        for domain, is_primary in BMP_HOSTS:
            r = await conn.execute(text(
                "SELECT 1 FROM tenant_domains WHERE domain = :d"
            ), {"d": domain})
            if r.scalar():
                continue
            await conn.execute(text(
                "INSERT INTO tenant_domains (tenant_id, domain, is_primary, created_at) "
                "VALUES (1, :d, :p, NOW())"
            ), {"d": domain, "p": is_primary})
            print(f"  + seeded {domain} -> tenant 1 (primary={is_primary})")

    print("Migration complete — tenant_domains ready.")


if __name__ == "__main__":
    asyncio.run(main())
