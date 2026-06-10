"""Recover from the May 30 – Jun 4 2026 contact-discovery outage.

During that window netrows_find_decision_makers failed silently (the
campaign pipeline swallowed every exception), so 900+ companies were
scraped, enriched, qualified on problems — and then skipped with
"No contact email". The Houston campaign completed with only 200 of its
potential and Naples completed with zero.

This script re-runs contact discovery (Netrows, then Hunter) for those
companies and stores the contacts. It does NOT enroll anyone in a
sequence — re-open the affected campaigns afterwards and the normal
batch runner re-walks its (location × vertical) pairs, finds the
now-discoverable contacts, and enrolls them under the campaign's own
daily caps and round-robin assignment.

Usage:
    python -m scripts.recover_lost_contact_discovery \
        [--since 2026-05-30] [--until 2026-06-05] [--limit 1000] [--dry-run]
"""
import argparse
import asyncio
import sys

from sqlalchemy import text

from app.database import async_session
from app.config import settings
from app.runtime_config import get_netrows_api_key
from app.routes.campaign_routes import _ensure_contact
from app.services.netrows_enrichment import find_decision_makers as netrows_find_decision_makers
from app.services.hunter_enrichment import search_domain as hunter_search


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-05-30")
    ap.add_argument("--until", default="2026-06-05")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    async with async_session() as db:
        nr_key = await get_netrows_api_key(db)
        rows = (await db.execute(text("""
            SELECT co.id, co.name, co.website, co.phone
            FROM companies co
            WHERE co.created_at BETWEEN :since AND :until
              AND co.enriched
              AND co.website IS NOT NULL AND co.website != ''
              AND COALESCE(json_array_length(co.problems_found::json), 0) >= 3
              AND NOT EXISTS (SELECT 1 FROM contacts ct WHERE ct.company_id = co.id
                              AND ct.email IS NOT NULL AND ct.email != '')
            ORDER BY co.id
            LIMIT :lim
        """), {"since": args.since, "until": args.until, "lim": args.limit})).fetchall()

    print(f"{len(rows)} companies to re-discover "
          f"(netrows_key={'set' if nr_key else 'MISSING'}, "
          f"hunter_key={'set' if settings.hunter_api_key else 'MISSING'})")
    if args.dry_run:
        for r in rows[:20]:
            print(" ", r.id, r.name, r.website)
        return 0

    found_email = no_email = errors = 0
    for i, r in enumerate(rows, 1):
        got = False
        async with async_session() as db:
            if nr_key:
                try:
                    nr = await netrows_find_decision_makers(r.website, nr_key)
                    for dm in nr.decision_makers:
                        if await _ensure_contact(db, r.id, dm.full_name, dm.email,
                                                 dm.job_title, r.phone, dm.linkedin_url):
                            got = got or bool(dm.email)
                except Exception as e:
                    errors += 1
                    print(f"  netrows failed for {r.name}: {str(e)[:80]}")
            if not got and settings.hunter_api_key:
                try:
                    hunter = await hunter_search(r.website, settings.hunter_api_key)
                    for hc in hunter.contacts:
                        if hc.email:
                            full = f"{hc.first_name or ''} {hc.last_name or ''}".strip()
                            if await _ensure_contact(db, r.id, full, hc.email,
                                                     hc.position, r.phone, None):
                                got = True
                except Exception as e:
                    errors += 1
                    print(f"  hunter failed for {r.name}: {str(e)[:80]}")
            await db.commit()
        if got:
            found_email += 1
        else:
            no_email += 1
        if i % 50 == 0:
            print(f"  {i}/{len(rows)} — emails found for {found_email}, "
                  f"none for {no_email}, errors {errors}")
        await asyncio.sleep(0.5)  # be polite to Netrows/Hunter

    print(f"DONE: {found_email} companies now have a contact email, "
          f"{no_email} still without, {errors} lookup errors")
    print("Next step: re-open the affected campaigns (status='running', "
          "indexes reset) so the batch runner enrolls the recovered companies "
          "under its normal daily caps.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
