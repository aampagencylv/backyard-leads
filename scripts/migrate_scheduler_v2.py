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
        sched = {r[1] for r in (await conn.execute(text("PRAGMA table_info(scheduling_configs)"))).fetchall()}
        for name, ddl in SCHEDULING_COLUMNS:
            if name not in sched:
                await conn.execute(text(f"ALTER TABLE scheduling_configs ADD COLUMN {name} {ddl}"))
                print(f"+ added scheduling_configs.{name}")

        bookings = {r[1] for r in (await conn.execute(text("PRAGMA table_info(bookings)"))).fetchall()}
        for name, ddl in BOOKINGS_COLUMNS:
            if name not in bookings:
                await conn.execute(text(f"ALTER TABLE bookings ADD COLUMN {name} {ddl}"))
                print(f"+ added bookings.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
