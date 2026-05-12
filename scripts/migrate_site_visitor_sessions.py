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
from app.services.migration_utils import table_exists
from app.database import engine


async def main() -> None:
    """No-op when the table already exists (created by SQLAlchemy
    Base.metadata.create_all on first boot). Historical SQLite-style
    DDL is kept here only for the legacy mid-session bootstrap path —
    Postgres deployments never hit it because create_all runs first."""
    async with engine.begin() as conn:
        if await table_exists(conn, "site_visitor_sessions"):
            return  # already created from the SQLAlchemy model
        # SQLite legacy path — only fires on a half-init'd dev DB.
        await conn.execute(text("""
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
        """))
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
