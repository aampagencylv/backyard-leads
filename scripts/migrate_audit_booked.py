"""
Add audit_reports.booked_at + booked_email so the iClosed webhook has
somewhere to write authoritative booking confirmations.

booked_email is set by the prospect via the self-confirm /unlock click.
booked_at is set by the iClosed webhook when a real time slot is locked.

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        if not await column_exists(conn, "audit_reports", "booked_at"):
            await conn.execute(text("ALTER TABLE audit_reports ADD COLUMN booked_at DATETIME"))
            print("+ added audit_reports.booked_at")
        if not await column_exists(conn, "audit_reports", "booked_email"):
            await conn.execute(text("ALTER TABLE audit_reports ADD COLUMN booked_email VARCHAR(255)"))
            print("+ added audit_reports.booked_email")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
