"""Add is_archived column to contacts table."""
import asyncio
from sqlalchemy import text
from app.database import engine

async def main():
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS is_archived BOOLEAN DEFAULT FALSE NOT NULL"))
        print("[migrate_is_archived] column ensured")

if __name__ == "__main__":
    asyncio.run(main())
