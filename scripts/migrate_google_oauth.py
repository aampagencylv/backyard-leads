"""
Add Google OAuth + native-scheduler columns to users.

  - google_email, google_refresh_token, google_calendar_id, google_connected_at
  - booking_slug (public booking URL slug)

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


COLUMNS = [
    ("google_email",          "TEXT"),
    ("google_refresh_token",  "TEXT"),
    ("google_calendar_id",    "TEXT"),
    ("google_connected_at",   "DATETIME"),
    ("booking_slug",          "TEXT"),
]


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(users)"))).fetchall()}
        for name, ddl in COLUMNS:
            if name not in cols:
                await conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {ddl}"))
                print(f"+ added users.{name}")
        # Unique index on booking_slug — partial index so multiple NULLs are allowed
        idx = (await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='ix_users_booking_slug'"
        ))).first()
        if not idx:
            await conn.execute(text(
                "CREATE UNIQUE INDEX ix_users_booking_slug "
                "ON users(booking_slug) WHERE booking_slug IS NOT NULL"
            ))
            print("+ added unique index ix_users_booking_slug")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
