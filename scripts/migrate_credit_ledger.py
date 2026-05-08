"""
Add the credit_ledger table for the credit meter shim.

Every billable action emits a row here:
  - credits_debited (customer-facing units)
  - raw_cost_usd    (what we actually pay vendors → admin COGS view)

Shim mode at launch — rows are written but nothing enforces a balance.
Lets us collect 1-2 weeks of real cost data before SaaS pricing.

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS credit_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    action_type VARCHAR(40) NOT NULL,
    action_ref VARCHAR(100),
    credits_debited INTEGER NOT NULL DEFAULT 0,
    raw_cost_usd FLOAT NOT NULL DEFAULT 0.0,
    vendor VARCHAR(40),
    idempotency_key VARCHAR(120) NOT NULL UNIQUE,
    metadata_json TEXT,
    created_at DATETIME NOT NULL
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_credit_ledger_user_id ON credit_ledger(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_credit_ledger_action_type ON credit_ledger(action_type)",
    "CREATE INDEX IF NOT EXISTS ix_credit_ledger_vendor ON credit_ledger(vendor)",
    "CREATE INDEX IF NOT EXISTS ix_credit_ledger_idempotency_key ON credit_ledger(idempotency_key)",
    "CREATE INDEX IF NOT EXISTS ix_credit_ledger_created_at ON credit_ledger(created_at)",
]


async def main() -> None:
    async with engine.begin() as conn:
        # Detect if the table exists by checking sqlite_master.
        existed = (await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='credit_ledger'")
        )).scalar_one_or_none()
        await conn.execute(text(CREATE_SQL))
        if not existed:
            print("+ created credit_ledger table")
        for idx_sql in INDEXES:
            await conn.execute(text(idx_sql))
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
