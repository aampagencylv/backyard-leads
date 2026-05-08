"""
Add activities.reply_sentiment + activities.reply_sentiment_summary so the
inbound-reply classifier has somewhere to write.

reply_sentiment ∈ {interested, objection, out_of_office, wrong_person,
unsubscribe, other} — populated async by reply_classifier.py.
reply_sentiment_summary is a one-line AI gist surfaced in the timeline.

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(activities)"))).fetchall()}
        if "reply_sentiment" not in cols:
            await conn.execute(text("ALTER TABLE activities ADD COLUMN reply_sentiment VARCHAR(20)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_activities_reply_sentiment ON activities(reply_sentiment)"))
            print("+ added activities.reply_sentiment + index")
        if "reply_sentiment_summary" not in cols:
            await conn.execute(text("ALTER TABLE activities ADD COLUMN reply_sentiment_summary TEXT"))
            print("+ added activities.reply_sentiment_summary")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
