"""Phase A foundation: create the tenants table and add tenant_id to every
tenant-owned table.

The strategy is intentionally additive and safe:
  - DEFAULT 1 means existing rows are implicitly backfilled to tenant #1
    (BMP) without an explicit UPDATE.
  - NOT NULL is enforced from the start; any code path that inserts a row
    without specifying tenant_id will get 1 (BMP). This keeps BMP running
    unchanged while we incrementally update app code to pass tenant_id
    explicitly in later commits.
  - Every step is idempotent (existence checks before DDL), so the
    migration is safe to chain in init_db and safe to re-run.

This commit does NOT change any application behavior. It only stages the
database for the tenant-aware code that comes next.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


# Every tenant-owned public-schema table in production at 2026-06-02.
# Verified via pg_tables — none currently has a tenant_id column.
TENANT_OWNED_TABLES = [
    "activities", "api_keys", "audit_log", "audit_reports", "bookings",
    "campaign_logs", "campaign_members", "campaign_runs", "campaign_targets",
    "campaigns", "companies", "company_tags", "contacts", "credit_ledger",
    "custom_field_definitions", "deals", "feedback", "generated_emails",
    "page_views", "pending_deletions", "runtime_config", "saved_views",
    "scheduling_configs", "searches", "sequence_templates",
    "site_visitor_sessions", "sos_lookups", "tags", "tasks",
    "tracking_links", "users", "webhooks",
]


async def _table_exists(conn, name: str) -> bool:
    r = await conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=:n)"
    ), {"n": name})
    return bool(r.scalar())


async def main() -> None:
    async with engine.begin() as conn:
        # 1. Create the tenants table if it doesn't exist.
        if not await _table_exists(conn, "tenants"):
            await conn.execute(text("""
                CREATE TABLE tenants (
                    id           SERIAL PRIMARY KEY,
                    name         VARCHAR(255) NOT NULL,
                    slug         VARCHAR(64)  NOT NULL UNIQUE,
                    status       VARCHAR(32)  NOT NULL DEFAULT 'active',
                    plan         VARCHAR(32)  NOT NULL DEFAULT 'starter',
                    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
                )
            """))
            print("+ created table tenants")

        # 2. Insert tenant #1 (BMP) if not already there.
        exists = await conn.execute(text("SELECT 1 FROM tenants WHERE id = 1"))
        if not exists.scalar():
            # Force id=1 explicitly; bump the sequence so future SERIAL picks 2.
            await conn.execute(text(
                "INSERT INTO tenants (id, name, slug, status, plan) "
                "VALUES (1, 'Backyard Marketing Pros', 'bmp', 'active', 'enterprise')"
            ))
            await conn.execute(text(
                "SELECT setval(pg_get_serial_sequence('tenants', 'id'), "
                "GREATEST((SELECT MAX(id) FROM tenants), 1))"
            ))
            print("+ inserted tenant #1 (Backyard Marketing Pros)")

        # 3. Add tenant_id to every tenant-owned table.
        # NOT NULL with DEFAULT 1 means existing rows backfill automatically
        # and any not-yet-updated code keeps writing rows under tenant #1.
        added = 0
        for table in TENANT_OWNED_TABLES:
            if not await _table_exists(conn, table):
                # Table doesn't exist in this environment — skip silently
                # (e.g., a future deployment may add tables not yet here).
                continue
            if await column_exists(conn, table, "tenant_id"):
                continue
            await conn.execute(text(
                f"ALTER TABLE {table} "
                f"ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1 "
                f"REFERENCES tenants(id)"
            ))
            await conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant_id "
                f"ON {table}(tenant_id)"
            ))
            added += 1
            print(f"  + {table}.tenant_id")
        if added:
            print(f"+ added tenant_id to {added} tables")

    print("Migration complete — multi-tenant foundation in place.")


if __name__ == "__main__":
    asyncio.run(main())
