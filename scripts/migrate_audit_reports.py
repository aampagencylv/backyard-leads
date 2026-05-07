"""Create audit_reports table. Idempotent."""
import asyncio
from sqlalchemy import text
from app.database import engine


async def migrate():
    async with engine.begin() as conn:
        tables = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='audit_reports'"))
        if not tables.fetchone():
            await conn.execute(text("""
                CREATE TABLE audit_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id INTEGER NOT NULL UNIQUE REFERENCES companies(id),
                    token VARCHAR(32) NOT NULL UNIQUE,
                    html_content TEXT NOT NULL,
                    ai_findability_score INTEGER DEFAULT 0,
                    content_citability_score INTEGER DEFAULT 0,
                    local_seo_score INTEGER DEFAULT 0,
                    overall_grade VARCHAR(2) DEFAULT '',
                    findings_json TEXT,
                    view_count INTEGER DEFAULT 0,
                    last_viewed_at DATETIME,
                    generated_at DATETIME
                )
            """))
            print("migrate_audit_reports: created audit_reports table")


if __name__ == "__main__":
    asyncio.run(migrate())
