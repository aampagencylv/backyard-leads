"""Revert the 10 companies auto-qualified purely by machine opens.

Security-gateway scanners opened tracking pixels within seconds of send;
the 3-distinct-opens rule then promoted these companies to 'qualified'
with zero human-speed opens (incident 2026-06-10, fixed by the
BOT_OPEN_WINDOW_SECONDS classifier). This reverts the auto-qualify side
effects for exactly the affected ids:

  - company.status 'qualified' → 'sequencing' (pending steps remain) or
    'contacted' (sequence finished). Companies a human already moved to
    another status are left alone.
  - strips the "[Auto-qualified: opened N distinct emails]" marker so a
    future LEGITIMATE 3-open streak can re-qualify them.
  - deals bounced in_sequence → 'qualified' by the auto-qualify are
    returned to in_sequence/value 0. Deals a rep moved further
    (proposal etc.) are untouched.
  - open "Follow up" tasks spawned by the false qualification are
    deleted so reps stop chasing phantom hot leads.

Usage: python -m scripts.revert_bot_qualified [--dry-run]
"""
import argparse
import asyncio
import re
import sys

from sqlalchemy import select, text

from app.database import async_session
from app.models import Company, Deal, Task

# Verified 2026-06-10: every open for these companies arrived <60s after
# send (see audit transcript) — zero plausibly-human opens.
PURE_BOT_COMPANY_IDS = [7, 137, 1050, 1059, 1153, 1184, 1209, 1480, 1520, 1561]

_MARKER_RE = re.compile(r" ?\[Auto-qualified: opened \d+ distinct emails\]")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    async with async_session() as db:
        companies = (await db.execute(
            select(Company).where(Company.id.in_(PURE_BOT_COMPANY_IDS))
        )).scalars().all()

        for co in companies:
            actions_pending = (await db.execute(text("""
                SELECT 1 FROM actions a
                JOIN engagements e ON e.id = a.engagement_id
                WHERE e.company_id = :co AND e.status = 'active'
                  AND a.status IN ('scheduled', 'paused', 'awaiting_approval')
                LIMIT 1
            """), {"co": co.id})).first() is not None
            legacy_pending = (await db.execute(text("""
                SELECT 1 FROM generated_emails
                WHERE company_id = :co AND is_sent = FALSE AND skipped_at IS NULL
                LIMIT 1
            """), {"co": co.id})).first() is not None

            new_status = co.status
            if co.status == "qualified":
                new_status = "sequencing" if (actions_pending or legacy_pending) else "contacted"

            stripped = _MARKER_RE.sub("", co.enrichment_summary or "")

            deals = (await db.execute(
                select(Deal).where(Deal.company_id == co.id, Deal.stage == "qualified")
            )).scalars().all()

            open_tasks = (await db.execute(
                select(Task).where(
                    Task.company_id == co.id,
                    Task.completed == False,
                    Task.description.like("Follow up%"),
                )
            )).scalars().all()

            print(f"#{co.id} {co.name}: status {co.status} -> {new_status}, "
                  f"marker={'present' if stripped != (co.enrichment_summary or '') else 'absent'}, "
                  f"deals_to_revert={len(deals)}, tasks_to_delete={len(open_tasks)}")

            if args.dry_run:
                continue

            co.status = new_status
            co.enrichment_summary = stripped
            for d in deals:
                d.stage = "in_sequence"
                d.probability = 0
                d.value = 0
            for t in open_tasks:
                await db.delete(t)

        if not args.dry_run:
            await db.commit()
            print(f"reverted {len(companies)} companies")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
