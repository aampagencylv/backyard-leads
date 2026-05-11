"""
Add org-wide brand columns to runtime_config — the single source of
truth for the tenant's identity. Emails, audit reports, booking pages,
and app UI accents all derive from these values.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
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
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(runtime_config)"))).fetchall()}
        for name, ddl in COLUMNS:
            if name not in cols:
                await conn.execute(text(f"ALTER TABLE runtime_config ADD COLUMN {name} {ddl}"))
                print(f"+ added runtime_config.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
