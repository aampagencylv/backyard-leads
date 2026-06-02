"""Phase A: Row-Level Security policies on every tenant-owned table.

Defense-in-depth: even if a route forgets to add `WHERE tenant_id = X`,
the database refuses to return rows that don't match the current
tenant context.

The policy reads `app.current_tenant_id` — a session GUC that the
`get_tenant_db` dependency sets per request. When the GUC is unset
or empty, the policy passes through (returns all rows). That means:

  * Today, before any route migrates to `get_tenant_db`, the GUC is
    never set → policy is a no-op → BMP behaves identically.
  * Once a route migrates to `get_tenant_db`, the GUC IS set →
    policy enforces tenant isolation.

We also FORCE row-level security so the policy applies to the table
owner too (the app connects as `postgres` on Supabase, which would
otherwise bypass RLS).

Idempotent — drops + recreates the policy each run so policy changes
land on redeploy.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


# Same set as migrate_multitenant_foundation.py
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

POLICY_NAME = "tenant_isolation"

# USING: which rows are visible for SELECT/UPDATE/DELETE
# WITH CHECK: which rows can be INSERTed or UPDATEd
#
# NULLIF(setting, '')::int handles both unset (NULL) and the empty-string
# value that set_config() returns when called with an empty value. When
# the GUC is unset, both branches' NULLIF(...) returns NULL, so the
# `IS NULL` branch passes — the policy is a no-op.
POLICY_EXPR = """(
    NULLIF(current_setting('app.current_tenant_id', true), '') IS NULL
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
)"""


async def _table_exists(conn, name: str) -> bool:
    r = await conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=:n)"
    ), {"n": name})
    return bool(r.scalar())


async def _column_exists(conn, table: str, column: str) -> bool:
    r = await conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=:t AND column_name=:c)"
    ), {"t": table, "c": column})
    return bool(r.scalar())


async def main() -> None:
    async with engine.begin() as conn:
        installed = 0
        for table in TENANT_OWNED_TABLES:
            if not await _table_exists(conn, table):
                continue
            if not await _column_exists(conn, table, "tenant_id"):
                # Skip until tenant_id column exists (migrate_multitenant_foundation
                # should have run first via init_db ordering).
                print(f"  ! skip {table}: no tenant_id column")
                continue

            # Ensure RLS is enabled + forced (owner can't bypass).
            await conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            await conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))

            # Drop-then-create makes this idempotent + lets us update the
            # policy expression in future redeploys without manual SQL.
            await conn.execute(text(
                f"DROP POLICY IF EXISTS {POLICY_NAME} ON {table}"
            ))
            await conn.execute(text(
                f"CREATE POLICY {POLICY_NAME} ON {table} "
                f"AS PERMISSIVE FOR ALL "
                f"USING {POLICY_EXPR} "
                f"WITH CHECK {POLICY_EXPR}"
            ))
            installed += 1

        print(f"+ RLS tenant_isolation policy on {installed} tables")
    print("Migration complete — RLS policies in place.")


if __name__ == "__main__":
    asyncio.run(main())
