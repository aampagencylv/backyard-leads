"""
Add users.onboarding_step for the guided 10-step product tour.

0 = not started → tour will auto-start on next login
1-10 = currently on this step
99 = skipped
100 = completed

New users (first login after this migration ships) get onboarding_step=0
and the tour fires automatically.
Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(users)"))).fetchall()}
        if "onboarding_step" not in cols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN onboarding_step INTEGER NOT NULL DEFAULT 0"))
            print("+ added users.onboarding_step")
            # Existing users (BMP team) skip the tour by default — they don't need it.
            # New signups get default=0 so the tour fires for them.
            await conn.execute(text("UPDATE users SET onboarding_step = 100 WHERE onboarding_step = 0"))
            print("+ marked existing users as onboarding-complete (they don't need the tour)")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
