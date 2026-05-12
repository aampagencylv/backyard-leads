"""Check if Resend webhook events are landing in the database."""
import asyncio
from sqlalchemy import select, func
from app.database import async_session
from app.models import Activity

async def main():
    async with async_session() as db:
        print("=== Email event activities ===")
        for ev in ('email_sent', 'email_delivered', 'email_opened', 'email_clicked', 'email_bounced', 'email_complained'):
            cnt = (await db.execute(select(func.count(Activity.id)).where(Activity.activity_type == ev))).scalar() or 0
            print(f"  {ev}: {cnt}")

        # Check the generated_emails table for delivery tracking fields
        from app.models import GeneratedEmail
        cols = [c.name for c in GeneratedEmail.__table__.columns]
        delivery_cols = [c for c in cols if 'deliver' in c.lower() or 'open' in c.lower() or 'click' in c.lower() or 'bounce' in c.lower()]
        print(f"\n=== GeneratedEmail delivery columns: {delivery_cols or 'NONE'} ===")

        # Check if there's an email_events table
        from sqlalchemy import text
        try:
            cnt = (await db.execute(text("SELECT COUNT(*) FROM email_events"))).scalar()
            print(f"\n=== email_events table: {cnt} rows ===")
            recent = (await db.execute(text("SELECT * FROM email_events ORDER BY id DESC LIMIT 5"))).fetchall()
            for r in recent:
                print(f"  {r}")
        except Exception as e:
            print(f"\n=== email_events table: {e} ===")

        # Most recent webhook-related activities
        print("\n=== Last 10 webhook-sourced activities ===")
        rows = (await db.execute(
            select(Activity)
            .where(Activity.activity_type.in_(('email_delivered', 'email_opened', 'email_clicked', 'email_bounced')))
            .order_by(Activity.created_at.desc()).limit(10)
        )).scalars().all()
        if rows:
            for a in rows:
                print(f"  {a.activity_type} company={a.company_id} at={a.created_at}")
        else:
            print("  NONE — no delivery events have been recorded yet")

asyncio.run(main())
