"""
Add Tier 2 Netrows cache columns to companies.

  - similarweb_json + similarweb_fetched_at + monthly_visits (denormalized)
  - tech_stack_json + tech_stack_fetched_at
  - yelp_json + yelp_fetched_at
  - indeed_jobs_json + indeed_jobs_fetched_at

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


COLUMNS = [
    ("similarweb_json",          "TEXT"),
    ("similarweb_fetched_at",    "DATETIME"),
    ("monthly_visits",           "INTEGER"),
    ("tech_stack_json",          "TEXT"),
    ("tech_stack_fetched_at",    "DATETIME"),
    ("yelp_json",                "TEXT"),
    ("yelp_fetched_at",          "DATETIME"),
    ("indeed_jobs_json",         "TEXT"),
    ("indeed_jobs_fetched_at",   "DATETIME"),
]


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(companies)"))).fetchall()}
        for name, ddl in COLUMNS:
            if name not in cols:
                await conn.execute(text(f"ALTER TABLE companies ADD COLUMN {name} {ddl}"))
                print(f"+ added companies.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
