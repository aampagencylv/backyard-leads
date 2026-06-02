"""Attach a sequence to runtime_config.id so SERIAL-style inserts work.

The column was originally created as a plain INTEGER PRIMARY KEY because
the ORM model carried `default=1` (singleton-row pattern). With the
multi-tenant refactor that default is gone — every tenant gets its own
row, and Postgres needs a sequence to auto-assign ids.

This migration:
  1. Creates `runtime_config_id_seq` if missing
  2. Bumps the sequence to MAX(id) + 1 (so it picks up where existing
     rows left off — BMP's row sits at id=1, next tenant gets id=2)
  3. Sets the column default to nextval(...) so future ORM inserts
     that omit `id` get an auto-assigned value
  4. Marks the sequence as OWNED BY the column so it tracks the
     table's lifecycle

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        # 1. Create the sequence
        await conn.execute(text(
            "CREATE SEQUENCE IF NOT EXISTS runtime_config_id_seq"
        ))
        # 2. Catch up to the current max
        await conn.execute(text(
            "SELECT setval('runtime_config_id_seq', "
            "GREATEST(COALESCE((SELECT MAX(id) FROM runtime_config), 0), 1))"
        ))
        # 3. Wire the sequence as the column default
        await conn.execute(text(
            "ALTER TABLE runtime_config "
            "ALTER COLUMN id SET DEFAULT nextval('runtime_config_id_seq'::regclass)"
        ))
        # 4. Bind sequence ownership to the column (so DROP TABLE drops the seq)
        await conn.execute(text(
            "ALTER SEQUENCE runtime_config_id_seq OWNED BY runtime_config.id"
        ))
    print("Migration complete — runtime_config.id is now auto-assigned.")


if __name__ == "__main__":
    asyncio.run(main())
