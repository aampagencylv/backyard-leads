"""
Create the page_views table — Phase 2 of Website Visitor Tracking.
The JS snippet on backyardmarketingpros.com sends a beacon to
/api/track/pageview which inserts a row here, attributed to the
contact via the bmp_visitor cookie.
Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        existing = (await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='page_views'"
        ))).scalar_one_or_none()
        if existing:
            print("page_views table already exists.")
            return
        await conn.execute(text("""
            CREATE TABLE page_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                visitor_token VARCHAR(32) NOT NULL,
                contact_id INTEGER REFERENCES contacts(id),
                company_id INTEGER REFERENCES companies(id),
                url TEXT NOT NULL,
                page_title VARCHAR(500),
                referrer TEXT,
                user_agent VARCHAR(300),
                ip VARCHAR(64),
                created_at DATETIME NOT NULL
            )
        """))
        await conn.execute(text("CREATE INDEX ix_page_views_token ON page_views(visitor_token)"))
        await conn.execute(text("CREATE INDEX ix_page_views_contact ON page_views(contact_id)"))
        await conn.execute(text("CREATE INDEX ix_page_views_created ON page_views(created_at)"))
        print("+ created page_views table + indexes")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
