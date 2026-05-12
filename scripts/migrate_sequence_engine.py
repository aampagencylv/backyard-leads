"""
Sequence engine v1: extend generated_emails with engine-execution columns.

The existing table already has step_type, scheduled_send_at, paused_at, is_sent —
we add the missing pieces: skip-if logic, skipped_at, auto_execute (does the engine
fire it automatically vs. create a BDR Task), sequence_label (to group main vs.
post-call sequences), payload_json (channel-specific data), and task_id (link to
the BDR Task created for non-auto steps).
Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


COLUMNS = [
    ("skip_if_json",    "TEXT"),
    ("skipped_at",      "DATETIME"),
    ("skip_reason",     "VARCHAR(80)"),
    ("auto_execute",    "BOOLEAN NOT NULL DEFAULT 0"),
    ("sequence_label",  "VARCHAR(40) NOT NULL DEFAULT 'main'"),
    ("payload_json",    "TEXT"),
    ("task_id",         "INTEGER"),
]


async def main() -> None:
    async with engine.begin() as conn:
        for name, ddl in COLUMNS:
            if not await column_exists(conn, "generated_emails", name):
                await conn.execute(text(f"ALTER TABLE generated_emails ADD COLUMN {name} {ddl}"))
                print(f"+ added generated_emails.{name}")
        # Backfill auto_execute on existing rows: emails were always auto-sendable
        # (human-clicked but the row represented a sendable email)
        await conn.execute(text("""
            UPDATE generated_emails
            SET auto_execute = 1
            WHERE step_type = 'email' AND (auto_execute IS NULL OR auto_execute = 0)
        """))
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
