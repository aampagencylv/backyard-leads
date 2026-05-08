"""
Create the native-scheduler tables: scheduling_configs + bookings.

Idempotent. Auto-runs on startup via init_db(). create_all() handles
brand-new tables fine, but we also CREATE INDEX IF NOT EXISTS the
critical query indexes here for clarity.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        # The tables themselves are created by Base.metadata.create_all
        # in init_db() — this script is a safety net to ensure the
        # supporting indexes exist even when tables are pre-created.
        for stmt in (
            "CREATE INDEX IF NOT EXISTS ix_bookings_host_starts "
            "ON bookings(host_user_id, starts_at)",
            "CREATE INDEX IF NOT EXISTS ix_bookings_email "
            "ON bookings(prospect_email)",
            "CREATE INDEX IF NOT EXISTS ix_bookings_company "
            "ON bookings(company_id)",
        ):
            await conn.execute(text(stmt))
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
