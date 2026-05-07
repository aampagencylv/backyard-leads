"""
Create the tracking_links table — Phase 1 of Website Visitor Tracking.
Each URL in outgoing emails gets wrapped through /t/{token} which logs
the click + drops the bmp_visitor cookie before redirecting to the
destination.
Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        existing = (await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tracking_links'"
        ))).scalar_one_or_none()
        if existing:
            print("tracking_links table already exists.")
            return
        await conn.execute(text("""
            CREATE TABLE tracking_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token VARCHAR(32) NOT NULL UNIQUE,
                contact_id INTEGER REFERENCES contacts(id),
                company_id INTEGER REFERENCES companies(id),
                email_id INTEGER REFERENCES generated_emails(id),
                destination_url TEXT NOT NULL,
                label VARCHAR(40),
                created_at DATETIME NOT NULL,
                first_clicked_at DATETIME,
                last_clicked_at DATETIME,
                click_count INTEGER NOT NULL DEFAULT 0
            )
        """))
        await conn.execute(text("CREATE INDEX ix_tracking_links_token ON tracking_links(token)"))
        await conn.execute(text("CREATE INDEX ix_tracking_links_contact ON tracking_links(contact_id)"))
        await conn.execute(text("CREATE INDEX ix_tracking_links_company ON tracking_links(company_id)"))
        print("+ created tracking_links table + indexes")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
