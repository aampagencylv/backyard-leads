"""
Add Twilio Voice fields to users + runtime_config tables.

Per-rep numbers store on users.twilio_phone_number + twilio_identity.
Twilio API credentials store on the runtime_config singleton so they
can be rotated from the Settings UI.

Idempotent: safe to run on every restart.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


COLUMNS = [
    ("users",          "twilio_phone_number",   "VARCHAR(40)"),
    ("users",          "twilio_identity",       "VARCHAR(80)"),
    ("runtime_config", "twilio_account_sid",    "TEXT"),
    ("runtime_config", "twilio_auth_token",     "TEXT"),
    ("runtime_config", "twilio_api_key_sid",    "TEXT"),
    ("runtime_config", "twilio_api_key_secret", "TEXT"),
    ("runtime_config", "twilio_twiml_app_sid",  "TEXT"),
]


async def _columns(conn, table: str) -> set[str]:
    rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
    return {row[1] for row in rows}


async def main() -> None:
    async with engine.begin() as conn:
        for table, name, ddl in COLUMNS:
            cols = await _columns(conn, table)
            if name not in cols:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                print(f"+ added {table}.{name}")

        # Backfill twilio_identity for any existing user that doesn't have one
        await conn.execute(text("""
            UPDATE users
            SET twilio_identity = 'bmp_user_' || id
            WHERE twilio_identity IS NULL
        """))
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
