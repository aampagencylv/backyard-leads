"""Add tasks.engagement_action_id so the new engagement engine can surface
BDR-handled actions (call_task, manual, linkedin) as legacy CRM tasks.

The link makes the new engine the SOLE source of truth for outreach:
  - New engine dispatcher creates an actions row
  - For BDR-handled channels, the channel adapter's send() inserts a
    matching tasks row with engagement_action_id pointing back
  - BDR sees the task in their existing CRM task view, exactly as today
  - BDR clicks Complete → both the task AND the action get marked done

UNIQUE constraint ensures one task per action — re-dispatch can't dupe
the row.

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("""
            ALTER TABLE tasks
              ADD COLUMN IF NOT EXISTS engagement_action_id BIGINT
        """))
        # UNIQUE WHERE NOT NULL — multiple legacy tasks with NULL link OK;
        # exactly one task per non-null engagement_action_id.
        await conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_engagement_action
              ON tasks (engagement_action_id)
              WHERE engagement_action_id IS NOT NULL
        """))
        # FK can't reference partitioned ai_decisions but actions is regular
        await conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'fk_tasks_engagement_action'
                ) THEN
                    ALTER TABLE tasks ADD CONSTRAINT fk_tasks_engagement_action
                    FOREIGN KEY (engagement_action_id) REFERENCES actions(id);
                END IF;
            END $$;
        """))
        print("+ tasks.engagement_action_id added (with FK + unique index)")


if __name__ == "__main__":
    asyncio.run(main())
