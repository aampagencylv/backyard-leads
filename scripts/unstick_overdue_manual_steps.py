"""One-time unstick: auto-skip manual sequence steps that have been overdue
more than N days (default 3).

Why: BDRs aren't always doing the manual LinkedIn/call steps that the
sequence schedules. Those stale steps clutter the ⚠ Stalled tab and make
sequences look broken when in fact the auto-emails downstream of them
are still firing on their own schedule (the engine processes auto-rows
and task-rows independently — there is no upstream dependency).

What we skip:
  - step_type in ('linkedin', 'call', 'imessage')
  - auto_execute = False (manual)
  - is_sent = False, skipped_at IS NULL, paused_at IS NULL
  - scheduled_send_at < now - N days
  - If a Task is linked and the Task is complete, we leave the step alone
    (the complete_task fix should have already marked it sent — anything
    still here is a true no-show).

Marks step.skipped_at + step.skip_reason = "manual_overdue_<N>d" and logs
a sequence_step_skipped Activity so the timeline tells the story.

Run with --apply to commit; default is dry-run. --days N to change the
cutoff (default 3).
"""
import asyncio
import sys
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, and_, or_
from app.database import async_session
from app.models import GeneratedEmail, Activity, Task


async def main(apply: bool, days: int):
    async with async_session() as db:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        skip_reason = f"manual_overdue_{days}d"

        candidates = (await db.execute(
            select(GeneratedEmail).where(
                GeneratedEmail.is_sent == False,
                GeneratedEmail.skipped_at.is_(None),
                GeneratedEmail.paused_at.is_(None),
                GeneratedEmail.auto_execute == False,
                GeneratedEmail.step_type.in_(("linkedin", "call", "imessage")),
                GeneratedEmail.scheduled_send_at < cutoff,
            ).order_by(GeneratedEmail.company_id, GeneratedEmail.sequence_order)
        )).scalars().all()

        # Filter out steps whose linked Task IS completed (those should have been
        # marked sent by the complete_task fix — if we see one, it's a legacy
        # row to leave alone, not skip).
        will_skip = []
        for step in candidates:
            if step.task_id:
                t = (await db.execute(select(Task).where(Task.id == step.task_id))).scalar_one_or_none()
                if t and t.completed:
                    continue
            will_skip.append(step)

        print(f"Cutoff: scheduled_send_at < {cutoff.isoformat()} ({days} days ago)")
        print(f"Found {len(will_skip)} manual steps to auto-skip "
              f"({len(candidates) - len(will_skip)} candidates excluded — linked task complete)\n")

        # Bucket by type for readability
        bucket: dict[str, int] = {}
        affected_companies: set[int] = set()
        for step in will_skip:
            key = f"{step.step_type}/{step.email_type or '?'}"
            bucket[key] = bucket.get(key, 0) + 1
            affected_companies.add(step.company_id)
        for k, v in sorted(bucket.items(), key=lambda x: -x[1]):
            print(f"  {k:<30} {v:>5}")
        print(f"\nAffected companies: {len(affected_companies)}")

        if not apply:
            print("\n(dry run — pass --apply to commit)")
            return

        # Apply: mark skipped + log activity
        for step in will_skip:
            step.skipped_at = now
            step.skip_reason = skip_reason
            db.add(Activity(
                company_id=step.company_id,
                contact_id=step.contact_id,
                activity_type="sequence_step_skipped",
                content=(
                    f"[Auto] Skipped {step.step_type} step #{step.sequence_order} — "
                    f"manual action overdue >{days}d. Downstream auto-steps continue."
                ),
            ))
        await db.commit()
        print(f"\nSkipped {len(will_skip)} steps + logged activities.")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    days = 3
    for i, a in enumerate(sys.argv):
        if a == "--days" and i + 1 < len(sys.argv):
            days = int(sys.argv[i + 1])
    asyncio.run(main(apply, days))
