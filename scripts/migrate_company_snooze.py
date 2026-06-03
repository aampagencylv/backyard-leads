"""Per-company sequence-snooze fields.

Lets a BDR pause an entire company's outbound sequence with a wake date.
Use case: the prospect said "not interested at this time, check back in
90 days." We don't want to disqualify (terminal) but we also don't want
to keep emailing/calling/texting them in the meantime.

On wake, the sequence engine regenerates a fresh tailored sequence
anchored at the wake date, with the first email referencing the agreed
timeframe ("You asked me to follow back up in 90 days — circling back").

All columns nullable — NULL = not snoozed.

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine
from app.services.migration_utils import column_exists


async def main() -> None:
    async with engine.begin() as conn:
        if not await column_exists(conn, "companies", "sequence_resume_at"):
            await conn.execute(text(
                "ALTER TABLE companies ADD COLUMN sequence_resume_at TIMESTAMPTZ NULL"
            ))
            print("+ companies.sequence_resume_at added")
        if not await column_exists(conn, "companies", "sequence_snooze_reason"):
            await conn.execute(text(
                "ALTER TABLE companies ADD COLUMN sequence_snooze_reason VARCHAR(500) NULL"
            ))
            print("+ companies.sequence_snooze_reason added")
        if not await column_exists(conn, "companies", "sequence_snoozed_at"):
            await conn.execute(text(
                "ALTER TABLE companies ADD COLUMN sequence_snoozed_at TIMESTAMPTZ NULL"
            ))
            print("+ companies.sequence_snoozed_at added")
        if not await column_exists(conn, "companies", "sequence_snoozed_by_user_id"):
            await conn.execute(text(
                "ALTER TABLE companies ADD COLUMN sequence_snoozed_by_user_id INTEGER NULL"
            ))
            print("+ companies.sequence_snoozed_by_user_id added")
        if not await column_exists(conn, "companies", "sequence_snooze_days"):
            # The original "N days" the BDR requested at snooze time.
            # Stored so the wake re-engagement email can reference it
            # verbatim ("you asked me to follow up in 30 days").
            await conn.execute(text(
                "ALTER TABLE companies ADD COLUMN sequence_snooze_days INTEGER NULL"
            ))
            print("+ companies.sequence_snooze_days added")

        # Index to make the engine's "find companies waking up now" query cheap.
        # Partial index — only rows with sequence_resume_at IS NOT NULL — keeps
        # the index tiny since 99% of companies are not snoozed at any moment.
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_companies_sequence_resume_at "
            "ON companies (sequence_resume_at) "
            "WHERE sequence_resume_at IS NOT NULL"
        ))
    print("Migration complete — company snooze fields ready.")


if __name__ == "__main__":
    asyncio.run(main())
