"""
Add event_type / event_label / event_value columns to page_views — extends
WVT to capture actions (form submits, outbound clicks, tel/mailto taps, custom
button clicks) in addition to bare pageviews. Hot-lead detection now triggers
on action signals not just page-count.
Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


COLUMNS = [
    ("event_type",  "VARCHAR(30) NOT NULL DEFAULT 'pageview'"),
    ("event_label", "VARCHAR(200)"),
    ("event_value", "TEXT"),
]


async def main() -> None:
    async with engine.begin() as conn:
        for name, ddl in COLUMNS:
            if not await column_exists(conn, "page_views", name):
                await conn.execute(text(f"ALTER TABLE page_views ADD COLUMN {name} {ddl}"))
                print(f"+ added page_views.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
