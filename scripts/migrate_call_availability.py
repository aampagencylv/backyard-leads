"""Add is_available_for_calls column to users table."""
import asyncio
from sqlalchemy import text
from app.database import engine

async def main():
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_available_for_calls BOOLEAN DEFAULT TRUE NOT NULL"))
        print("[migrate_call_availability] column ensured")

if __name__ == "__main__":
    asyncio.run(main())
