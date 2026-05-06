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
from app.database import engine


COLUMNS = [
    ("contacts",  "recent_posts_json",     "TEXT"),
    ("contacts",  "posts_fetched_at",      "DATETIME"),
    ("companies", "google_place_id",       "VARCHAR(80)"),
    ("companies", "reviews_json",          "TEXT"),
    ("companies", "reviews_fetched_at",    "DATETIME"),
]


async def _columns(conn, table: str) -> set[str]:
    rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
    return {row[1] for row in rows}


async def main() -> None:
    async with engine.begin() as conn:
        for table, name, ddl in COLUMNS:
            cols = await _columns(conn, table)
            if name not in cols:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                print(f"+ added {table}.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
