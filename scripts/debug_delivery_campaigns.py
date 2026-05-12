"""Debug: check email delivery status and campaign health via the app's DB."""
import asyncio
from sqlalchemy import select, func, text
from app.database import async_session
from app.models import GeneratedEmail, Contact, Company, Activity, Campaign, User

async def main():
    async with async_session() as db:
        print("=== Email status overview ===")
        total = (await db.execute(select(func.count(GeneratedEmail.id)))).scalar() or 0
        sent = (await db.execute(select(func.count(GeneratedEmail.id)).where(GeneratedEmail.is_sent == True))).scalar() or 0
        pending = (await db.execute(select(func.count(GeneratedEmail.id)).where(
            GeneratedEmail.is_sent == False, GeneratedEmail.paused_at.is_(None), GeneratedEmail.skipped_at.is_(None)
        ))).scalar() or 0
        paused = (await db.execute(select(func.count(GeneratedEmail.id)).where(GeneratedEmail.paused_at.isnot(None)))).scalar() or 0
        skipped = (await db.execute(select(func.count(GeneratedEmail.id)).where(GeneratedEmail.skipped_at.isnot(None)))).scalar() or 0
        print(f"  Total: {total}  Sent: {sent}  Pending: {pending}  Paused: {paused}  Skipped: {skipped}")

        print("\n=== Last 10 emails (any status) ===")
        rows = (await db.execute(
            select(GeneratedEmail, Contact.email, Company.name)
            .outerjoin(Contact, GeneratedEmail.contact_id == Contact.id)
            .outerjoin(Company, GeneratedEmail.company_id == Company.id)
            .order_by(GeneratedEmail.id.desc()).limit(10)
        )).all()
        for ge, to_email, co_name in rows:
            status = "SENT" if ge.is_sent else ("PAUSED" if ge.paused_at else ("SKIPPED" if ge.skipped_at else "PENDING"))
            print(f"  #{ge.id} {ge.step_type} {status} auto={ge.auto_execute} to={to_email} co={co_name}")
            print(f"    scheduled={ge.scheduled_send_at} subject={(ge.subject or '')[:50]}")
            if ge.skip_reason: print(f"    skip_reason={ge.skip_reason}")

        print("\n=== Email activity events ===")
        for ev_type in ('email_sent', 'email_delivered', 'email_opened', 'email_clicked', 'email_bounced', 'email_replied'):
            cnt = (await db.execute(select(func.count(Activity.id)).where(Activity.activity_type == ev_type))).scalar() or 0
            if cnt > 0:
                print(f"  {ev_type}: {cnt}")
        sent_acts = (await db.execute(select(func.count(Activity.id)).where(Activity.activity_type == 'email_sent'))).scalar() or 0
        if sent_acts == 0:
            print("  NO email_sent activities found")

        print("\n=== Campaigns ===")
        campaigns = (await db.execute(select(Campaign).order_by(Campaign.id.desc()).limit(5))).scalars().all()
        if campaigns:
            for c in campaigns:
                print(f"\n  Campaign #{c.id}: {c.name}")
                print(f"    status={c.status} mode={c.mode}")
                print(f"    prospects_today={c.prospects_today} max_per_day={c.max_prospects_per_day}")
                print(f"    loc_idx={c.current_location_index}")
                print(f"    created={c.created_at}")
                # Check targets
                try:
                    from app.models import CampaignTarget
                    tcount = (await db.execute(select(func.count(CampaignTarget.id)).where(CampaignTarget.campaign_id == c.id))).scalar() or 0
                    print(f"    targets: {tcount}")
                except Exception as e:
                    print(f"    targets: error - {e}")
        else:
            print("  No campaigns found")

        print("\n=== Sequence engine recent errors (from server log) ===")
        errors = (await db.execute(
            select(Activity).where(Activity.activity_type.like('%error%')).order_by(Activity.created_at.desc()).limit(5)
        )).scalars().all()
        if errors:
            for e in errors:
                print(f"  {e.activity_type}: {(e.content or '')[:100]} at={e.created_at}")
        else:
            print("  No error activities")

        print(f"\n=== Counts: companies={await _count(db, Company)} contacts={await _count(db, Contact)} users={await _count(db, User)} ===")

async def _count(db, model):
    return (await db.execute(select(func.count(model.id)))).scalar() or 0

asyncio.run(main())
