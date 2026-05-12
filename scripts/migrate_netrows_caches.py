"""
Add Netrows-related cache columns to existing tables.

Idempotent: safe to run on every restart.

Adds:
  contacts.recent_posts_json     TEXT
  contacts.posts_fetched_at      DATETIME
  companies.google_place_id      VARCHAR(80)
  companies.reviews_json         TEXT
  companies.reviews_fetched_at   DATETIME

Usage:
    python -m scripts.migrate_netrows_caches
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


COLUMNS = [
    ("contacts",  "recent_posts_json",     "TEXT"),
    ("contacts",  "posts_fetched_at",      "DATETIME"),
    ("companies", "google_place_id",       "VARCHAR(80)"),
    ("companies", "reviews_json",          "TEXT"),
    ("companies", "reviews_fetched_at",    "DATETIME"),
]


async def main() -> None:
    async with engine.begin() as conn:
        for table, name, ddl in COLUMNS:
            if not await column_exists(conn, table, name):
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                print(f"+ added {table}.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
