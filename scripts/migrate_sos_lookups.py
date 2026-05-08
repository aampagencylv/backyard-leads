"""
Add sos_lookups cache table for Secretary-of-State enrichment.

Public-record scrapes are free but slow + state ToS varies, so cache
aggressively. 30-day TTL — SoS filings change rarely.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS sos_lookups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    state VARCHAR(4) NOT NULL,
    company_name VARCHAR(255) NOT NULL,
    found BOOLEAN NOT NULL DEFAULT 0,
    result_json TEXT,
    fetched_at DATETIME NOT NULL,
    expires_at DATETIME
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_sos_lookups_state ON sos_lookups(state)",
    "CREATE INDEX IF NOT EXISTS ix_sos_lookups_company_name ON sos_lookups(company_name)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_sos_lookups_state_name ON sos_lookups(state, company_name)",
    "CREATE INDEX IF NOT EXISTS ix_sos_lookups_expires_at ON sos_lookups(expires_at)",
]


async def main() -> None:
    async with engine.begin() as conn:
        existed = (await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='sos_lookups'")
        )).scalar_one_or_none()
        await conn.execute(text(CREATE_SQL))
        if not existed:
            print("+ created sos_lookups table")
        for idx in INDEXES:
            await conn.execute(text(idx))
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
