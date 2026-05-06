"""
Add users.role column and saved_views table.
Idempotent — safe to run multiple times.
"""
import asyncio
from sqlalchemy import text
from app.database import engine


async def migrate():
    async with engine.begin() as conn:
        # Add role column to users
        cols = await conn.execute(text("PRAGMA table_info(users)"))
        col_names = [r[1] for r in cols.fetchall()]

        if "role" not in col_names:
            await conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'sales_rep'"))
            # Make the first user an admin
            await conn.execute(text("""
                UPDATE users SET role = 'admin'
                WHERE id = (SELECT MIN(id) FROM users)
            """))
            print("migrate_roles_and_views: added users.role, first user set to admin")

        # Create saved_views table
        tables = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='saved_views'"))
        if not tables.fetchone():
            await conn.execute(text("""
                CREATE TABLE saved_views (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    page VARCHAR(30) NOT NULL,
                    name VARCHAR(100) NOT NULL,
                    filters_json TEXT NOT NULL,
                    created_at DATETIME
                )
            """))
            print("migrate_roles_and_views: created saved_views table")


if __name__ == "__main__":
    asyncio.run(migrate())
