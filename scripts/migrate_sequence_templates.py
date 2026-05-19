"""Seed the sequence_templates table with the DEFAULT_30DAY_TEMPLATE so
the admin sequence builder has something to read out of the box.

The CREATE TABLE itself is handled by Base.metadata.create_all (SQLAlchemy
sees the new model in app/models.py). This migration only seeds the
default row when the table is empty — idempotent.

Chained from init_db().
"""
from __future__ import annotations
import asyncio
import json
from datetime import datetime, timezone
from sqlalchemy import text
from app.database import engine


# Same shape as DEFAULT_30DAY_TEMPLATE in app/services/sequence_engine.py.
# Duplicated here so the migration is self-contained — the engine constant
# remains the runtime fallback if the DB ever returns no rows.
DEFAULT_STEPS = [
    {"day": 0,  "step_type": "email",     "label": "cold",             "skip_if": ["no_email", "opted_out"], "auto": True},
    {"day": 3,  "step_type": "linkedin",  "label": "linkedin_connect", "skip_if": ["no_linkedin"],            "auto": False},
    {"day": 5,  "step_type": "call",      "label": "call_1",           "skip_if": [],                         "auto": False},
    {"day": 7,  "step_type": "email",     "label": "follow_up_1",      "skip_if": ["no_email", "opted_out"], "auto": True},
    {"day": 9,  "step_type": "imessage",  "label": "imessage_1",       "skip_if": ["no_phone", "opted_out", "landline"], "auto": True},
    {"day": 12, "step_type": "call",      "label": "call_2",           "skip_if": [],                         "auto": False},
    {"day": 15, "step_type": "email",     "label": "follow_up_2",      "skip_if": ["no_email", "opted_out"], "auto": True},
    {"day": 18, "step_type": "imessage",  "label": "imessage_2",       "skip_if": ["no_phone", "opted_out", "landline"], "auto": True},
    {"day": 20, "step_type": "linkedin",  "label": "linkedin_message", "skip_if": ["no_linkedin"],            "auto": False},
    {"day": 23, "step_type": "call",      "label": "call_3",           "skip_if": [],                         "auto": False},
    {"day": 26, "step_type": "imessage",  "label": "imessage_3",       "skip_if": ["no_phone", "opted_out", "landline"], "auto": True},
    {"day": 28, "step_type": "email",     "label": "breakup",          "skip_if": ["no_email", "opted_out"], "auto": True},
    {"day": 30, "step_type": "call",      "label": "call_final",       "skip_if": [],                         "auto": False},
]


async def main() -> None:
    async with engine.begin() as conn:
        # Check if any rows exist
        result = await conn.execute(text("SELECT COUNT(*) FROM sequence_templates"))
        count = result.scalar() or 0
        if count > 0:
            print(f"sequence_templates already has {count} row(s) — skip seed.")
            return

        now = datetime.now(timezone.utc)
        await conn.execute(
            text("""
                INSERT INTO sequence_templates
                  (name, is_active, is_default, steps_json, auto_skip_days, auto_resume_days, created_at, updated_at)
                VALUES
                  (:name, :is_active, :is_default, :steps_json, :auto_skip_days, :auto_resume_days, :now, :now)
            """),
            {
                "name": "30-day default",
                "is_active": True,
                "is_default": True,
                "steps_json": json.dumps(DEFAULT_STEPS),
                "auto_skip_days": 3,
                "auto_resume_days": 0,
                "now": now,
            },
        )
        print("seeded sequence_templates: 30-day default (13 steps)")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
