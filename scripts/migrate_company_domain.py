"""
Add companies.domain column + backfill existing rows from website.

Steve hit a real case 2026-05-07 where two `AAMP Agency` rows existed in
the DB with the same website. This column is the dedupe key — every new
company creation looks up the normalized domain first and reuses the
existing row instead of inserting a duplicate.
Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine
from app.services.domain_utils import normalize_domain


async def main() -> None:
    async with engine.begin() as conn:
        if not await column_exists(conn, "companies", "domain"):
            await conn.execute(text("ALTER TABLE companies ADD COLUMN domain VARCHAR(255)"))
            print("+ added companies.domain")
        # Index for fast dedupe lookup (idempotent — IF NOT EXISTS)
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_companies_domain ON companies(domain)"))

        # Backfill existing rows. Safe to re-run — only writes when current domain differs.
        rows = (await conn.execute(text("SELECT id, website, domain FROM companies"))).fetchall()
        updated = 0
        for r in rows:
            cid, website, current_domain = r[0], r[1], r[2]
            new_domain = normalize_domain(website)
            if new_domain != current_domain:
                await conn.execute(
                    text("UPDATE companies SET domain = :d WHERE id = :id"),
                    {"d": new_domain, "id": cid},
                )
                updated += 1
        if updated:
            print(f"+ backfilled domain on {updated} companies")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
