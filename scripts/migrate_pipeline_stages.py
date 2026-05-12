"""
Pipeline stage customization — adds a JSON-blob column to runtime_config
where admins can configure the middle stages of the deal pipeline
(qualified / proposal / negotiation, or whatever they rename them to).

System stages (in_sequence, closed_won, closed_lost, snoozed) are NEVER
configurable — they have special wiring (sequence engine auto-sets
in_sequence; revenue calc reads closed_won; snooze flow has its own
restore logic). Only the editable middle is stored here.

  runtime_config:
    - pipeline_stages_json (TEXT, nullable — NULL means "use defaults")

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


COLUMNS = [
    ("pipeline_stages_json", "TEXT"),
]


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(runtime_config)"))).fetchall()}
        for name, ddl in COLUMNS:
            if name not in cols:
                await conn.execute(text(f"ALTER TABLE runtime_config ADD COLUMN {name} {ddl}"))
                print(f"+ added runtime_config.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
