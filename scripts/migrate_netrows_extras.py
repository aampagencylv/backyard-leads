"""
Add cache columns for Netrows premium endpoints (Phase 2 of the audit
of the 273-endpoint Netrows API surface).

  company_insights_json + insights_fetched_at        — /companies/insights
  instagram_posts_json  + instagram_posts_fetched_at — /instagram/user/posts

Both auto-fire during /companies/{id}/enrich; both also have manual
refresh endpoints. JSON-serialized so the schema doesn't have to churn
whenever Netrows adds a field.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


COLUMNS = [
    ("company_insights_json",      "TEXT"),
    ("insights_fetched_at",        "DATETIME"),
    ("instagram_posts_json",       "TEXT"),
    ("instagram_posts_fetched_at", "DATETIME"),
]


async def main() -> None:
    async with engine.begin() as conn:
        for name, ddl in COLUMNS:
            if not await column_exists(conn, "companies", name):
                await conn.execute(text(f"ALTER TABLE companies ADD COLUMN {name} {ddl}"))
                print(f"+ added companies.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
