"""
Email event timestamps on generated_emails:

  - delivered_at  (DATETIME, nullable) — Resend email.delivered
  - opened_at     (DATETIME, nullable) — first email.opened
  - open_count    (INTEGER NOT NULL DEFAULT 0) — bumps on each open
  - bounced_at    (DATETIME, nullable) — Resend email.bounced
  - complained_at (DATETIME, nullable) — Resend email.complained

Backfills the existing is_sent → delivered_at where possible so the
reputation dashboard has historical baseline.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


COLUMNS = [
    ("delivered_at",  "DATETIME"),
    ("opened_at",     "DATETIME"),
    ("open_count",    "INTEGER NOT NULL DEFAULT 0"),
    ("bounced_at",    "DATETIME"),
    ("complained_at", "DATETIME"),
]


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(generated_emails)"))).fetchall()}
        added: list[str] = []
        for name, ddl in COLUMNS:
            if name not in cols:
                await conn.execute(text(f"ALTER TABLE generated_emails ADD COLUMN {name} {ddl}"))
                added.append(name)
                print(f"+ added generated_emails.{name}")
        # Backfill delivered_at from sent_at where we know the email actually
        # left the system. sent_at is set when we POST to Resend successfully,
        # so it's a close-enough proxy for "delivered" until real webhook
        # events start filling the column accurately.
        if "delivered_at" in added:
            await conn.execute(text(
                "UPDATE generated_emails SET delivered_at = sent_at "
                "WHERE is_sent = 1 AND sent_at IS NOT NULL AND delivered_at IS NULL"
            ))
            print("+ backfilled delivered_at from sent_at on previously-sent emails")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
