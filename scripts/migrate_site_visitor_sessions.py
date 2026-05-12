"""
Website visitor identification — Phase 1 schema.

Creates the site_visitor_sessions table for anonymous visitors who
arrive on a tracked site without coming through an email link. The
visitor_resolver service hits an IP-to-company API (defaults to
IPInfo Lite, free tier) and backfills resolved_company_* fields so
returning visits attribute to the matched company.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS site_visitor_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bvid VARCHAR(64) NOT NULL UNIQUE,
    ip VARCHAR(64),
    user_agent VARCHAR(300),
    resolved_company_id INTEGER REFERENCES companies(id),
    resolved_company_name VARCHAR(255),
    resolved_domain VARCHAR(255),
    resolved_at DATETIME,
    is_isp_ip INTEGER NOT NULL DEFAULT 0,
    country VARCHAR(8),
    region VARCHAR(80),
    city VARCHAR(120),
    pageview_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at DATETIME NOT NULL DEFAULT (datetime('now')),
    last_seen_at DATETIME NOT NULL DEFAULT (datetime('now'))
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_site_visitor_sessions_bvid ON site_visitor_sessions(bvid)",
    "CREATE INDEX IF NOT EXISTS ix_site_visitor_sessions_ip ON site_visitor_sessions(ip)",
    "CREATE INDEX IF NOT EXISTS ix_site_visitor_sessions_resolved_company_id ON site_visitor_sessions(resolved_company_id)",
    "CREATE INDEX IF NOT EXISTS ix_site_visitor_sessions_resolved_domain ON site_visitor_sessions(resolved_domain)",
    "CREATE INDEX IF NOT EXISTS ix_site_visitor_sessions_first_seen_at ON site_visitor_sessions(first_seen_at)",
    "CREATE INDEX IF NOT EXISTS ix_site_visitor_sessions_last_seen_at ON site_visitor_sessions(last_seen_at)",
]


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(text(CREATE_TABLE))
        for ix in INDEXES:
            await conn.execute(text(ix))
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
