"""
Add org-wide brand columns to runtime_config — the single source of
truth for the tenant's identity. Emails, audit reports, booking pages,
and app UI accents all derive from these values.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


COLUMNS = [
    ("brand_primary_color",   "TEXT NOT NULL DEFAULT '#E65100'"),
    ("brand_secondary_color", "TEXT NOT NULL DEFAULT '#1B5E20'"),
    ("brand_accent_bg_color", "TEXT NOT NULL DEFAULT '#FFF8F0'"),
    ("brand_logo_url",        "TEXT"),
    ("brand_company_name",    "TEXT NOT NULL DEFAULT 'Backyard Marketing Pros'"),
]


async def main() -> None:
    async with engine.begin() as conn:
        for name, ddl in COLUMNS:
            if not await column_exists(conn, "runtime_config", name):
                await conn.execute(text(f"ALTER TABLE runtime_config ADD COLUMN {name} {ddl}"))
                print(f"+ added runtime_config.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
