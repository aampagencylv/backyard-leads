"""
Native scheduler v2 columns:

  scheduling_configs:
    - meeting_type (default 'google_meet')
    - meeting_location_details (TEXT, nullable)
    - booking_questions_json (TEXT, nullable; JSON array)

  bookings:
    - answers_json (TEXT, nullable; JSON object keyed by question.key)
    - google_meet_link (TEXT, nullable)

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


SCHEDULING_COLUMNS = [
    ("meeting_type",              "TEXT NOT NULL DEFAULT 'google_meet'"),
    ("meeting_location_details",  "TEXT"),
    ("booking_questions_json",    "TEXT"),
]

BOOKINGS_COLUMNS = [
    ("answers_json",       "TEXT"),
    ("google_meet_link",   "TEXT"),
]


async def main() -> None:
    async with engine.begin() as conn:
        for name, ddl in SCHEDULING_COLUMNS:
            if not await column_exists(conn, "scheduling_configs", name):
                await conn.execute(text(f"ALTER TABLE scheduling_configs ADD COLUMN {name} {ddl}"))
                print(f"+ added scheduling_configs.{name}")
        for name, ddl in BOOKINGS_COLUMNS:
            if not await column_exists(conn, "bookings", name):
                await conn.execute(text(f"ALTER TABLE bookings ADD COLUMN {name} {ddl}"))
                print(f"+ added bookings.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
