"""
Migrate the users table from name/title/phone/signature to
first_name/last_name/nickname/phone_number/scheduling_url.

Idempotent: safe to run on every app start.
Requires SQLite >= 3.35 for ALTER TABLE DROP COLUMN.

Usage:
    python -m scripts.migrate_signature_fields
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text

from app.database import engine


NEW_COLUMNS = [
    ("first_name",     "VARCHAR(80)  NOT NULL DEFAULT ''"),
    ("last_name",      "VARCHAR(80)  NOT NULL DEFAULT ''"),
    ("nickname",       "VARCHAR(120) NOT NULL DEFAULT ''"),
    ("phone_number",   "VARCHAR(40)  NOT NULL DEFAULT ''"),
    ("scheduling_url", "VARCHAR(255) NOT NULL DEFAULT ''"),
]
OLD_COLUMNS = ["name", "title", "phone", "signature"]


async def _existing_columns(conn) -> set[str]:
    rows = (await conn.execute(text("PRAGMA table_info(users)"))).fetchall()
    return {row[1] for row in rows}


async def main() -> None:
    async with engine.begin() as conn:
        cols = await _existing_columns(conn)

        # Add new columns (skip ones that already exist)
        for name, ddl in NEW_COLUMNS:
            if name not in cols:
                await conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {ddl}"))
                print(f"+ added users.{name}")

        # Best-effort backfill from old fields
        if "name" in cols:
            await conn.execute(text("""
                UPDATE users
                SET first_name = CASE
                        WHEN instr(trim(name), ' ') > 0
                        THEN substr(trim(name), 1, instr(trim(name), ' ') - 1)
                        ELSE trim(name)
                    END,
                    last_name = CASE
                        WHEN instr(trim(name), ' ') > 0
                        THEN substr(trim(name), instr(trim(name), ' ') + 1)
                        ELSE ''
                    END
                WHERE (first_name IS NULL OR first_name = '')
                  AND name IS NOT NULL AND name != ''
            """))
            print("  backfilled first_name/last_name from name")

        if "title" in cols:
            await conn.execute(text("""
                UPDATE users
                SET nickname = COALESCE(title, '')
                WHERE (nickname IS NULL OR nickname = '')
                  AND title IS NOT NULL AND title != ''
            """))
            print("  backfilled nickname from title")

        if "phone" in cols:
            await conn.execute(text("""
                UPDATE users
                SET phone_number = COALESCE(phone, '')
                WHERE (phone_number IS NULL OR phone_number = '')
                  AND phone IS NOT NULL AND phone != ''
            """))
            print("  backfilled phone_number from phone")

        # Drop old columns (re-read in case prior step altered the table)
        cols_after = await _existing_columns(conn)
        for old in OLD_COLUMNS:
            if old in cols_after:
                await conn.execute(text(f"ALTER TABLE users DROP COLUMN {old}"))
                print(f"- dropped users.{old}")

    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
