"""
Add generated_emails.sent_by_user_id for per-sender daily send-cap enforcement.

Deliverability protection: high-volume sends from a single From-address tank
inbox placement. Cap = 50/sender/day by default (configurable via env). The
engine defers a step to tomorrow's 8am if today's cap is hit.
Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        if not await column_exists(conn, "generated_emails", "sent_by_user_id"):
            await conn.execute(text("ALTER TABLE generated_emails ADD COLUMN sent_by_user_id INTEGER"))
            print("+ added generated_emails.sent_by_user_id")
        # Index for the daily-count query
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_generated_emails_sent_by_day "
            "ON generated_emails(sent_by_user_id, sent_at) "
            "WHERE is_sent = 1"
        ))
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
