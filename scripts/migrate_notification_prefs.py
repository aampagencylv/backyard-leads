"""Add notification_prefs_json column to users table."""
import asyncio
from sqlalchemy import text
from app.database import engine

async def main():
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS notification_prefs_json TEXT"))
        print("[migrate_notification_prefs] column ensured")

if __name__ == "__main__":
    asyncio.run(main())
