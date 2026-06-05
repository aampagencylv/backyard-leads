"""Seed `observations` rows so signal_watcher polls prospect websites.

The engagement engine's signal_watcher cron runs every 5 minutes and
processes one batch of observations. Until observations exist, the
worker is a no-op. This script populates one `website_homepage`
observation per active company that has a website — turning the
dormant signal_watcher into a working "react to website changes"
capability.

What gets seeded:
  - companies WHERE website IS NOT NULL AND status != 'not_interested'
  - one observation per (tenant_id, company_id) — primary contact attached
  - source_url = company.website (normalized to https://)
  - source_type_id = 5 (website_homepage)
  - is_active = TRUE, next_poll_at = NOW() + jittered offset so we don't
    hit a thundering-herd on the first poll wave
  - poll_interval_days = 14 (the source's default)

Idempotent: skips companies that already have an active observation
of this source_type.

Signal flow once seeded:
  signal_watcher tick (every 5m) → claim due observations →
    WebsiteHomepageSource.fetch() → store snapshot →
    extract_signals(prev, current) → emit `website_change` signals →
    decision_maker (Phase 4) reacts → optional action enqueued

Usage:
    python -m scripts.seed_website_observations [--dry-run] [--limit N]
                                                [--jitter-hours H]
"""
from __future__ import annotations
import argparse
import asyncio
import random
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from sqlalchemy import text

from app.database import async_session


# Pseudo-random but deterministic-by-company-id: same seeding run twice
# stamps the same next_poll_at, which lets the script be safely rerun
# without the cron worker being surprised by jumping windows.
def _normalize_url(raw: str) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    if not parsed.netloc:
        return None
    return raw[:500]


async def main(*, dry_run: bool, limit: int, jitter_hours: float) -> int:
    print(f"=== seed_website_observations ===")
    print(f"  dry_run={dry_run} limit={limit} jitter_hours={jitter_hours}")

    now = datetime.now(timezone.utc)
    rng = random.Random(now.date().toordinal())

    async with async_session() as db:
        # Resolve website_homepage source_type_id
        st_row = (await db.execute(text(
            "SELECT id FROM source_types WHERE code = 'website_homepage'"
        ))).first()
        if st_row is None:
            print("ERROR: source_types.website_homepage row missing")
            return 1
        source_type_id = int(st_row[0])

        # Companies with websites + status != not_interested, joined to
        # their primary contact (signal_watcher needs contact_id for
        # signal routing). Skip companies that already have an active
        # website_homepage observation.
        rows = (await db.execute(text("""
            SELECT c.id AS company_id, c.tenant_id, c.website,
                   ct.id AS contact_id
            FROM companies c
            LEFT JOIN LATERAL (
                SELECT id FROM contacts ct
                WHERE ct.company_id = c.id
                ORDER BY ct.is_primary DESC, ct.id ASC
                LIMIT 1
            ) ct ON TRUE
            WHERE c.website IS NOT NULL
              AND c.website <> ''
              AND c.status != 'not_interested'
              AND ct.id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM observations o
                  WHERE o.company_id = c.id
                    AND o.source_type_id = :sti
                    AND o.is_active = TRUE
              )
            ORDER BY c.id
            LIMIT :lim
        """), {"sti": source_type_id, "lim": limit})).fetchall()

    print(f"  found {len(rows)} company(s) needing seed")

    inserted = 0
    skipped = 0
    for r in rows:
        url = _normalize_url(r.website)
        if not url:
            skipped += 1
            continue

        # Spread next_poll_at across `jitter_hours` so the first poll
        # wave is distributed, not synchronized to NOW. Deterministic
        # per-company-id so reruns stamp identical times (safe).
        jitter_minutes = (hash((int(r.company_id), now.date())) % int(jitter_hours * 60))
        next_poll = now + timedelta(minutes=jitter_minutes)

        if dry_run:
            inserted += 1
            continue

        async with async_session() as db:
            await db.execute(text("""
                INSERT INTO observations (
                    tenant_id, contact_id, company_id,
                    source_type_id, source_url,
                    next_poll_at, poll_interval_days,
                    is_active, consecutive_failures,
                    created_at, updated_at
                )
                VALUES (
                    :t, :c, :co,
                    :sti, :url,
                    :next_poll, 14,
                    TRUE, 0,
                    NOW(), NOW()
                )
                ON CONFLICT DO NOTHING
            """), {
                "t": int(r.tenant_id), "c": int(r.contact_id),
                "co": int(r.company_id),
                "sti": source_type_id, "url": url,
                "next_poll": next_poll,
            })
            await db.commit()
        inserted += 1
        if inserted % 100 == 0:
            print(f"  ... {inserted}/{len(rows)} seeded")

    print()
    print(f"Done: inserted={inserted} skipped={skipped}")
    print()
    print(f"signal_watcher cron runs every 5m. First fetches will start "
          f"within {int(jitter_hours * 60)} minutes; results visible in "
          f"/var/log/eed-watcher.log on the VPS.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--jitter-hours", type=float, default=24.0,
                        help="Spread the first-poll wave across this many hours")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(
        dry_run=args.dry_run, limit=args.limit,
        jitter_hours=args.jitter_hours,
    )))
