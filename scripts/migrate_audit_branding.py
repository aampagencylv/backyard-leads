"""
Add audit-report branding columns to runtime_config so super_admins can
swap the header banner + footer logo on AI Findability reports without
a deploy.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


COLUMNS = [
    ("audit_report_header_url", "TEXT"),
    ("audit_report_logo_url",   "TEXT"),
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
