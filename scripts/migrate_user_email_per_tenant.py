"""Make user email unique PER TENANT instead of globally.

The pre-multitenant schema created a GLOBAL unique index on users.email
(ix_users_email). In the multi-tenant platform the same person can hold an
account in more than one tenant (the operator runs BMP and AAMP both), and
the auth layer is already built for it:

  - /login (auth_routes.py) resolves via get_tenant_db, so the lookup is
    auto-scoped to one tenant by host.
  - /universal-login loops every same-email candidate and matches by
    password ("first match wins").
  - admin create_tenant_user scopes its existence check by tenant_id.

The only thing blocking a same-email second account was the leftover global
unique index. This migration drops it and replaces it with a composite
UNIQUE (tenant_id, email), plus a plain (non-unique) index on email so the
cross-tenant /universal-login lookup stays fast.

Safe: a globally-unique column is trivially unique per (tenant_id, email),
so no existing row violates the new constraint.

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def _index_exists(conn, name: str) -> bool:
    r = await conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM pg_indexes "
        "WHERE schemaname='public' AND tablename='users' AND indexname=:n)"
    ), {"n": name})
    return bool(r.scalar())


async def main() -> None:
    async with engine.begin() as conn:
        # 1. Drop the global unique index if it's still there.
        if await _index_exists(conn, "ix_users_email"):
            await conn.execute(text("DROP INDEX IF EXISTS ix_users_email"))
            print("- dropped global unique index ix_users_email")

        # 2. Composite UNIQUE (tenant_id, email). Use a unique index (not a
        #    table constraint) so IF NOT EXISTS makes it idempotent.
        if not await _index_exists(conn, "uq_users_tenant_email"):
            await conn.execute(text(
                "CREATE UNIQUE INDEX uq_users_tenant_email "
                "ON users (tenant_id, email)"
            ))
            print("+ created composite unique index uq_users_tenant_email")

        # 3. Plain (non-unique) index on email for the cross-tenant
        #    universal-login lookup (WHERE email = :e, no tenant filter).
        if not await _index_exists(conn, "ix_users_email_nonunique"):
            await conn.execute(text(
                "CREATE INDEX ix_users_email_nonunique ON users (email)"
            ))
            print("+ created non-unique index ix_users_email_nonunique")

        print("user email is now unique per tenant")


if __name__ == "__main__":
    asyncio.run(main())
