"""Find all stalled auto-execute steps and re-anchor them to fire now.

A step is "stalled" when:
  - auto_execute = True (email or iMessage)
  - is_sent = False
  - not paused, not skipped
  - scheduled_send_at is in the past

This script re-anchors those steps so the sequence engine picks them up
on the next tick. Also reports any non-auto steps (call/linkedin) that
are overdue but leaves them alone (they're manual tasks).
"""
import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func
from app.database import async_session
from app.models import GeneratedEmail, Contact, Company

async def main():
    async with async_session() as db:
        now = datetime.now(timezone.utc)

        # Find stalled auto-execute steps
        stalled_auto = (await db.execute(
            select(GeneratedEmail, Contact.email, Company.name)
            .outerjoin(Contact, GeneratedEmail.contact_id == Contact.id)
            .outerjoin(Company, GeneratedEmail.company_id == Company.id)
            .where(
                GeneratedEmail.auto_execute == True,
                GeneratedEmail.is_sent == False,
                GeneratedEmail.paused_at.is_(None),
                GeneratedEmail.skipped_at.is_(None),
                GeneratedEmail.scheduled_send_at != None,
                GeneratedEmail.scheduled_send_at < now,
            )
            .order_by(GeneratedEmail.scheduled_send_at)
        )).all()

        print(f"=== Stalled auto-execute steps: {len(stalled_auto)} ===")

        # Group by company for readability
        by_company = {}
        for ge, contact_email, company_name in stalled_auto:
            key = company_name or f"company_{ge.company_id}"
            by_company.setdefault(key, []).append((ge, contact_email))

        reanchored = 0
        for company_name, steps in by_company.items():
            print(f"\n  {company_name}:")
            # Re-anchor: spread steps out starting from now, respecting
            # their relative day offsets
            base_day = min(s.send_delay_days or 0 for s, _ in steps)
            for ge, contact_email in steps:
                old = ge.scheduled_send_at
                offset_days = (ge.send_delay_days or 0) - base_day
                ge.scheduled_send_at = now + timedelta(days=max(offset_days, 0), minutes=reanchored % 10)
                reanchored += 1
                print(f"    #{ge.id} {ge.step_type} → {contact_email} was={old.strftime('%m/%d')} now={ge.scheduled_send_at.strftime('%m/%d %H:%M')}")

        await db.commit()
        print(f"\n=== Re-anchored {reanchored} stalled steps ===")

        # Report overdue manual tasks (not fixing, just reporting)
        overdue_manual = (await db.execute(
            select(func.count(GeneratedEmail.id))
            .where(
                GeneratedEmail.auto_execute == False,
                GeneratedEmail.is_sent == False,
                GeneratedEmail.paused_at.is_(None),
                GeneratedEmail.skipped_at.is_(None),
                GeneratedEmail.task_id.is_(None),
                GeneratedEmail.scheduled_send_at != None,
                GeneratedEmail.scheduled_send_at < now,
            )
        )).scalar() or 0
        print(f"\nOverdue manual tasks (call/linkedin — not touched): {overdue_manual}")

        # Snap to send window
        if reanchored > 0:
            print("\nSnapping to send window...")
            from app.services.send_window import snap_pending_steps_to_window
            # Get unique contact IDs that were re-anchored
            contact_ids = set()
            for steps_list in by_company.values():
                for ge, _ in steps_list:
                    if ge.contact_id:
                        contact_ids.add(ge.contact_id)
            for cid in contact_ids:
                try:
                    await snap_pending_steps_to_window(db, contact_id=cid)
                except Exception:
                    pass
            await db.commit()
            print("Done — steps snapped to configured send window")

asyncio.run(main())
