"""One-time backfill: collapse duplicate email_clicked activities + re-score.

The track_click endpoint historically created one Activity per TrackingLink
first-click. A single email typically has the same destination URL wrapped
through multiple tokens (logo, body, signature, footer) — and email-client
link prefetchers (Apple Mail Privacy Protection, Outlook SafeLinks, Gmail
proxy) fire every link the moment a message arrives, producing N "first
clicks" on N different tokens for the same email.

Paired with the tracking_routes fix that now dedupes per (email_id, contact)
at write time, this script cleans up the existing inflated rows by:

  1. Keeping the OLDEST email_clicked Activity per (contact_id, email_id)
  2. Deleting the rest
  3. Force-recomputing lead_score on every company that had any cleanup
     (so inflated hot/burning tiers drop back to reality)

Dry-run by default. Pass --apply to commit.
"""
import asyncio
import sys
from sqlalchemy import select, text, delete
from app.database import async_session
from app.models import Activity, Company
from app.services.lead_scorer import get_or_recompute


async def main(apply: bool):
    async with async_session() as db:
        # 1. Identify duplicate email_clicked rows. The metadata_json column is
        #    plain text JSON; cast to jsonb to extract email_id reliably.
        #    For each (contact, email) pair, keep the oldest row.
        dup_rows = (await db.execute(text("""
            WITH ranked AS (
                SELECT id, company_id, contact_id,
                       (metadata_json::jsonb->>'email_id')::int AS email_id,
                       created_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY contact_id, (metadata_json::jsonb->>'email_id')::int
                           ORDER BY created_at ASC, id ASC
                       ) AS rn
                FROM activities
                WHERE activity_type = 'email_clicked'
                  AND metadata_json IS NOT NULL
            )
            SELECT id, company_id, contact_id, email_id, created_at
            FROM ranked WHERE rn > 1 ORDER BY company_id, contact_id, email_id, created_at
        """))).all()

        affected_companies = sorted({r[1] for r in dup_rows if r[1]})
        print(f"Found {len(dup_rows)} duplicate email_clicked Activity rows across {len(affected_companies)} companies\n")

        if dup_rows:
            for r in dup_rows[:10]:
                print(f"  activity #{r[0]:>5}  company={r[1]:>4}  contact={r[2]:>4}  email={r[3]}  at {r[4]}")
            if len(dup_rows) > 10:
                print(f"  ... and {len(dup_rows) - 10} more")

        if not apply:
            print("\n(dry run — pass --apply to delete and re-score)")
            return

        # 2. Delete the duplicates.
        ids_to_delete = [r[0] for r in dup_rows]
        if ids_to_delete:
            await db.execute(delete(Activity).where(Activity.id.in_(ids_to_delete)))
            await db.commit()
            print(f"\nDeleted {len(ids_to_delete)} duplicate email_clicked Activity rows")

        # 3. Force-recompute lead score on every affected company.
        rescored = 0
        for cid in affected_companies:
            co = (await db.execute(select(Company).where(Company.id == cid))).scalar_one_or_none()
            if not co:
                continue
            before_tier = co.lead_score_tier
            before_score = co.lead_score
            result = await get_or_recompute(db, co, force=True)
            rescored += 1
            if before_tier != result.tier:
                print(f"  rescored company {cid:>4}: {before_score}→{result.combined}  {before_tier}→{result.tier}  {co.name[:50]}")

        print(f"\nRe-scored {rescored} companies after dedupe")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    asyncio.run(main(apply))
