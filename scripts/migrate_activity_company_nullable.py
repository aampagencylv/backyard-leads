"""Make activities.company_id nullable so calls placed against numbers
that aren't in the CRM (standalone Dial a Number) can still be
recorded for dashboard counts.

Per-company timeline views filter by company_id so they naturally
exclude orphan calls. The team dashboard counts by user_id only.

Idempotent on Postgres; SQLite has no NOT NULL drop and ignores the
constraint at insert time anyway, so it's a no-op there.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        try:
            row = (await conn.execute(text("""
                SELECT is_nullable FROM information_schema.columns
                WHERE table_name = 'activities' AND column_name = 'company_id'
            """))).first()
            if row and row[0] == "NO":
                await conn.execute(text("ALTER TABLE activities ALTER COLUMN company_id DROP NOT NULL"))
                print("+ activities.company_id is now nullable")
            else:
                pass  # already nullable or non-postgres
        except Exception as e:
            print(f"  (skipped: {type(e).__name__})")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
