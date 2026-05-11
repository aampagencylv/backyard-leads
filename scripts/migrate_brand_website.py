"""
Org brand v2 — adds the homepage URL field used in the email signature
+ compliance footer so the whole signature surface is white-label-ready.

  runtime_config:
    - brand_website_url (TEXT NOT NULL DEFAULT 'https://backyardmarketingpros.com')

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


COLUMNS = [
    ("brand_website_url", "TEXT NOT NULL DEFAULT 'https://backyardmarketingpros.com'"),
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
