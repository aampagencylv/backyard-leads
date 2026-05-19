"""One-time backfill: mark sequence steps sent when their linked Task is already complete.

Pairs with the crm_routes.complete_task fix that now propagates task completion
back to the linked GeneratedEmail row. This script catches the rows that
slipped through before the fix shipped.

Use --dry-run to preview; default is read-only preview, pass --apply to write.
"""
import asyncio
import sys
from datetime import timezone
from sqlalchemy import select
from app.database import async_session
from app.models import GeneratedEmail, Task, Company, Contact


async def main(apply: bool):
    async with async_session() as db:
        rows = (await db.execute(
            select(GeneratedEmail, Task, Company.name, Contact.first_name, Contact.last_name)
            .join(Task, Task.id == GeneratedEmail.task_id)
            .join(Company, Company.id == GeneratedEmail.company_id)
            .join(Contact, Contact.id == GeneratedEmail.contact_id)
            .where(
                GeneratedEmail.is_sent == False,
                GeneratedEmail.skipped_at.is_(None),
                Task.completed == True,
            )
            .order_by(Company.name, GeneratedEmail.sequence_order)
        )).all()

        print(f"Found {len(rows)} sequence steps where linked Task is complete but step is_sent=False\n")
        for step, task, company_name, fn, ln in rows:
            print(f"  step #{step.id:>5} {step.step_type:<10} {step.email_type or '':<22} "
                  f"order={step.sequence_order:>2} task #{task.id:>4} completed={task.completed_at} "
                  f"— {company_name} / {fn or ''} {ln or ''}".rstrip())

        if not apply:
            print("\n(dry run — pass --apply to update)")
            return

        updated = 0
        for step, task, _cn, _fn, _ln in rows:
            ts = task.completed_at
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            step.is_sent = True
            step.sent_at = ts
            updated += 1
        await db.commit()
        print(f"\nUpdated {updated} sequence steps to is_sent=True (sent_at = task.completed_at)")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    asyncio.run(main(apply))
