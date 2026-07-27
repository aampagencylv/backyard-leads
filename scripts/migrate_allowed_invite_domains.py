"""Add runtime_config.allowed_invite_domains_json — an OPTIONAL per-tenant
allowlist of email domains an admin may invite.

Replaces a hardcoded global allowlist (["backyardmarketingpros.com",
"aamp.agency"]) in POST /api/auth/users/invite, which meant any other tenant
literally could not invite their own staff.

Empty list = no restriction, which is the default for every tenant including
existing ones. Invites are already gated on the admin role and scoped to the
inviting tenant, so this list is a typo/hygiene guard rather than a security
boundary — defaulting it closed would just recreate the bug.

Nullable TEXT holding a JSON array. Idempotent.
"""
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
        if not await _col(conn, "runtime_config", "allowed_invite_domains_json"):
            await conn.execute(text(
                "ALTER TABLE runtime_config "
                "ADD COLUMN allowed_invite_domains_json TEXT DEFAULT '[]'"
            ))
            print("+ added runtime_config.allowed_invite_domains_json")
        else:
            print("= runtime_config.allowed_invite_domains_json already exists")

        # Normalise any NULLs to an explicit empty list so readers never have
        # to distinguish "unset" from "no restriction" — they mean the same.
        res = await conn.execute(text(
            "UPDATE runtime_config SET allowed_invite_domains_json = '[]' "
            "WHERE allowed_invite_domains_json IS NULL"
        ))
        if getattr(res, "rowcount", 0):
            print(f"= normalised {res.rowcount} NULL row(s) to '[]' (no restriction)")

        print("✓ Migration complete")


if __name__ == "__main__":
    asyncio.run(main())
