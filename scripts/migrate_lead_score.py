"""
Add lead_score columns to companies table for the v2 fit×intent scoring
model. Replaces the old "3+ opens or any click" Hot Leads heuristic with
a real per-company score driven by firmographics + decayed engagement
signals + reply sentiment + phone line type.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


COLUMNS = [
    ("lead_score", "INTEGER NOT NULL DEFAULT 0"),
    ("lead_score_fit", "INTEGER NOT NULL DEFAULT 0"),
    ("lead_score_intent", "INTEGER NOT NULL DEFAULT 0"),
    ("lead_score_tier", "VARCHAR(20) NOT NULL DEFAULT 'cold'"),
    ("lead_score_components", "TEXT"),
    ("lead_score_updated_at", "DATETIME"),
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_companies_lead_score ON companies(lead_score)",
    "CREATE INDEX IF NOT EXISTS ix_companies_lead_score_tier ON companies(lead_score_tier)",
]


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(companies)"))).fetchall()}
        for name, ddl in COLUMNS:
            if name not in cols:
                await conn.execute(text(f"ALTER TABLE companies ADD COLUMN {name} {ddl}"))
                print(f"+ added companies.{name}")
        for idx_sql in INDEXES:
            await conn.execute(text(idx_sql))
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
