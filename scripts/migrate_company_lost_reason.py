"""Fix two long-running 500s:

1) Add companies.lost_reason. The Unqualify-with-reason feature reads/writes
   it but the column was never added. Causes 500 on /api/companies/pending-review
   and /api/companies/{id}/disqualify.

2) Widen page_views.visitor_token from VARCHAR(32) to VARCHAR(64). Anonymous
   visitor tokens are 'anon-' + UUIDv4 = 41 chars, which exceeded the column
   width and 500'd the /api/track/pageview beacon for first-time visitors.

Both idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        if not await column_exists(conn, "companies", "lost_reason"):
            await conn.execute(text("ALTER TABLE companies ADD COLUMN lost_reason VARCHAR(500)"))
            print("+ added companies.lost_reason VARCHAR(500)")

        # Detect current width of page_views.visitor_token. Postgres-only path
        # via information_schema; SQLite has no concept of declared widths so
        # the no-op there is fine.
        try:
            row = (await conn.execute(text("""
                SELECT character_maximum_length FROM information_schema.columns
                WHERE table_name = 'page_views' AND column_name = 'visitor_token'
            """))).first()
            current_width = row[0] if row else None
            if current_width is not None and current_width < 64:
                await conn.execute(text("ALTER TABLE page_views ALTER COLUMN visitor_token TYPE VARCHAR(64)"))
                print(f"+ widened page_views.visitor_token VARCHAR({current_width}) -> VARCHAR(64)")
        except Exception as e:
            # SQLite or any non-Postgres engine — width concept doesn't apply
            # the same way; SQLAlchemy ignores declared widths there too.
            print(f"  (skipped visitor_token widen: {type(e).__name__})")

    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
