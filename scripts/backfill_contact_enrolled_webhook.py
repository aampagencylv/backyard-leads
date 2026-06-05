"""One-time backfill: fire `contact.enrolled` for the existing prospect list.

When the contact.enrolled webhook event was added, it only fires for
NEW enrollments going forward. To push the existing 946-contact cohort
into the Zapier-driven Meta/Google/LinkedIn audiences, this script
walks the active engagements and re-fires the same payload shape that
lifecycle.start_engagement would have produced — so the subscribed
Zapier hook receives them and pushes them into the configured custom
audiences.

Idempotent in the sense that re-running just fires again. Zapier
dedup (if any) is on the subscriber side. The dispatcher writes
last_delivery_at on the Webhook row — you can monitor progress in
real time via:

    watch -n2 "psql ... -c 'SELECT last_delivery_at, last_delivery_status, failure_count FROM webhooks'"

Filtering (default behavior — flip the flags below to change):
  - engagement.status = 'active' (skip terminal/declined contacts)
  - contact.email IS NOT NULL AND email != '' (must have a match key)
  - contact.unsubscribed_at IS NULL (CAN-SPAM + ad-platform compliance)
  - contact.do_not_contact = FALSE
  - contact.do_not_text = FALSE (less critical for email-focused
    audiences but worth excluding for SMS-overlapping campaigns)

Rate limiting: defaults to 5 events/sec so we don't trip Zapier's
catch-hook throttle (Pro plan: ~30/sec; Free: ~10/sec — 5 is safe
everywhere). At 5/sec, 946 contacts = ~3 minutes.

Usage:
    python -m scripts.backfill_contact_enrolled_webhook --dry-run
    python -m scripts.backfill_contact_enrolled_webhook --limit 50
    python -m scripts.backfill_contact_enrolled_webhook
    python -m scripts.backfill_contact_enrolled_webhook --include-terminal
"""
from __future__ import annotations
import argparse
import asyncio
import sys
import time
from sqlalchemy import text

from app.database import async_session


async def fetch_eligible(
    *,
    include_terminal: bool,
    include_no_email: bool,
    include_suppressed: bool,
    limit: int,
) -> list[dict]:
    """Pull every eligible (contact, company, BDR) row in one query.
    Matches the shape lifecycle.start_engagement would have fired."""
    where_clauses = []
    if not include_terminal:
        where_clauses.append("e.status = 'active'")
    if not include_no_email:
        where_clauses.append("c.email IS NOT NULL AND c.email != ''")
    if not include_suppressed:
        where_clauses.append("c.unsubscribed_at IS NULL")
        where_clauses.append("c.do_not_contact = FALSE")
        where_clauses.append("c.do_not_text = FALSE")
    where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

    sql = f"""
        SELECT
            e.id AS engagement_id, e.tenant_id, e.current_playbook_id,
            e.started_at,
            c.id AS contact_id, c.first_name, c.last_name, c.email,
            c.phone, c.phone_type, c.phone_carrier, c.title, c.linkedin_url,
            c.email_status, c.timezone, c.notes,
            c.unsubscribed_at, c.do_not_text, c.do_not_contact, c.is_primary,
            co.id AS company_id, co.name AS company_name, co.website, co.domain,
            co.phone AS company_phone, co.address, co.city, co.state,
            co.business_type, co.industry, co.company_size, co.employee_count,
            co.founded, co.company_description,
            co.linkedin_url AS company_linkedin,
            co.facebook_url, co.instagram_url, co.youtube_url, co.tiktok_url,
            co.lead_score, co.lead_score_tier, co.google_place_id,
            co.rating, co.review_count,
            COALESCE(e.assigned_bdr_id, co.assigned_to) AS bdr_id,
            u.email AS bdr_email, u.first_name AS bdr_first, u.last_name AS bdr_last,
            (SELECT COUNT(*) FROM actions a
             WHERE a.engagement_id = e.id) AS actions_count
        FROM engagements e
        JOIN contacts c ON c.id = e.contact_id
        JOIN companies co ON co.id = c.company_id
        LEFT JOIN users u ON u.id = COALESCE(e.assigned_bdr_id, co.assigned_to)
        WHERE {where_sql}
        ORDER BY e.id
        LIMIT :lim
    """

    async with async_session() as db:
        rows = (await db.execute(text(sql), {"lim": limit})).fetchall()
    return [dict(r._mapping) for r in rows]


def build_payload(d: dict) -> dict:
    bdr_name = " ".join(
        p for p in [d.get("bdr_first"), d.get("bdr_last")] if p
    ).strip() or None
    full_name = " ".join(
        p for p in [d.get("first_name"), d.get("last_name")] if p
    ).strip() or None
    return {
        "tenant_id": d["tenant_id"],
        "engagement_id": d["engagement_id"],
        "contact": {
            "id": d["contact_id"],
            "first_name": d["first_name"],
            "last_name": d["last_name"],
            "full_name": full_name,
            "email": d["email"],
            "email_status": d["email_status"],
            "phone": d["phone"],
            "phone_type": d["phone_type"],
            "phone_carrier": d["phone_carrier"],
            "title": d["title"],
            "linkedin_url": d["linkedin_url"],
            "timezone": d["timezone"],
            "is_primary": bool(d["is_primary"]),
            "notes": d["notes"],
            "unsubscribed": bool(d["unsubscribed_at"]),
            "do_not_text": bool(d["do_not_text"]),
            "do_not_contact": bool(d["do_not_contact"]),
        },
        "company": {
            "id": d["company_id"],
            "name": d["company_name"],
            "website": d["website"],
            "domain": d["domain"],
            "phone": d["company_phone"],
            "address": d["address"],
            "city": d["city"],
            "state": d["state"],
            "country": "US",
            "business_type": d["business_type"],
            "industry": d["industry"],
            "company_size": d["company_size"],
            "employee_count": d["employee_count"],
            "founded": d["founded"],
            "company_description": d["company_description"],
            "linkedin_url": d["company_linkedin"],
            "facebook_url": d["facebook_url"],
            "instagram_url": d["instagram_url"],
            "youtube_url": d["youtube_url"],
            "tiktok_url": d["tiktok_url"],
            "lead_score": d["lead_score"],
            "lead_score_tier": d["lead_score_tier"],
            "google_place_id": d["google_place_id"],
            "rating": d["rating"],
            "review_count": d["review_count"],
        },
        "assigned_bdr": (
            {
                "id": d["bdr_id"],
                "email": d["bdr_email"],
                "name": bdr_name,
            }
            if d["bdr_id"]
            else None
        ),
        "playbook_id": d["current_playbook_id"],
        "actions_count": d["actions_count"] or 0,
        "started_at": (
            d["started_at"].isoformat() if d["started_at"] else None
        ),
        "backfill": True,  # so Zapier can filter or tag the source
    }


async def main(args) -> int:
    print("=== backfill_contact_enrolled_webhook ===")
    print(f"  dry_run={args.dry_run}")
    print(f"  limit={args.limit}")
    print(f"  rate_per_second={args.rate_per_second}")
    print(f"  include_terminal={args.include_terminal}")
    print(f"  include_no_email={args.include_no_email}")
    print(f"  include_suppressed={args.include_suppressed}")
    print()

    rows = await fetch_eligible(
        include_terminal=args.include_terminal,
        include_no_email=args.include_no_email,
        include_suppressed=args.include_suppressed,
        limit=args.limit,
    )
    print(f"Eligible engagements: {len(rows)}")
    if not rows:
        print("Nothing to backfill.")
        return 0

    if args.dry_run:
        # Show first 3 sample payloads + summary
        import json
        print()
        print("--- sample payloads (first 3) ---")
        for d in rows[:3]:
            payload = build_payload(d)
            print(json.dumps(payload, indent=2, default=str))
            print()
        print(f"Would fire {len(rows)} events at {args.rate_per_second}/sec "
              f"(~{len(rows) / max(1, args.rate_per_second):.0f} sec total).")
        return 0

    # Fire each event with rate limiting. Each dispatch_event is
    # fire-and-forget (background asyncio task), so the loop pacing
    # IS the rate limit — actual HTTP requests to Zapier happen
    # concurrently in the background.
    from app.services.webhook_dispatch import dispatch_event

    interval = 1.0 / max(0.1, float(args.rate_per_second))
    print(f"Firing at {args.rate_per_second}/sec (interval={interval:.3f}s)")
    print()

    fired_total = 0
    delivered_total = 0
    start = time.monotonic()

    for i, d in enumerate(rows, start=1):
        payload = build_payload(d)
        # Each dispatch opens its own session to avoid sharing state
        async with async_session() as db:
            n = await dispatch_event(db, "contact.enrolled", payload)
        fired_total += 1
        delivered_total += n
        if i % 25 == 0:
            elapsed = time.monotonic() - start
            rate = i / max(0.001, elapsed)
            print(f"  ... {i}/{len(rows)} fired ({rate:.1f}/sec actual, "
                  f"{(len(rows) - i) / max(0.1, rate):.0f}s remaining)")
        await asyncio.sleep(interval)

    elapsed = time.monotonic() - start
    print()
    print(f"Done: fired={fired_total} dispatched_to_hooks={delivered_total} "
          f"elapsed={elapsed:.1f}s ({fired_total / max(0.1, elapsed):.1f}/sec)")
    print()
    print("Background HTTP deliveries to Zapier may continue for a few seconds. "
          "Check webhook delivery status via:")
    print("  SELECT last_delivery_at, last_delivery_status, failure_count FROM webhooks;")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be fired; don't actually dispatch")
    p.add_argument("--limit", type=int, default=10000,
                   help="Max engagements to process this run (default 10000)")
    p.add_argument("--rate-per-second", type=float, default=5.0,
                   help="Events per second (default 5; Zapier Pro tier safely "
                        "handles 30/sec, Free handles ~10/sec)")
    p.add_argument("--include-terminal", action="store_true",
                   help="Also push contacts whose engagement is terminal (declined). "
                        "Default: skip (no point spending ad $ on rejected prospects).")
    p.add_argument("--include-no-email", action="store_true",
                   help="Also push contacts with no email. Default: skip (email "
                        "is the primary match key on Meta + Google).")
    p.add_argument("--include-suppressed", action="store_true",
                   help="Also push unsubscribed / do_not_contact / do_not_text "
                        "contacts. STRONGLY discouraged — CAN-SPAM + ad-platform "
                        "compliance issue. Default: skip.")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args)))
