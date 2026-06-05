"""One-time backfill: classify reply sentiments for existing data.

Targets:
  1. signals.email_reply / sms_reply rows whose raw_data_json has no
     'sentiment' field (the bulk of new-engine traffic since cutover).
  2. activities with reply_sentiment IS NULL and activity_type in
     ('email_replied', 'sms_inbound') (legacy Resend Inbound + Twilio
     inbound rows from before classifier wiring).

Uses the same `app.services.reply_classifier.classify_reply` as the
inbound webhook path so the result is consistent. Metered as
`ai_reply_classify`.

Safe to run multiple times — only rows with NULL sentiment are touched.

Usage:
    python -m scripts.backfill_reply_classifications [--dry-run] [--limit N]

By default runs the whole backlog. --limit caps total classifications
this run (resumable since we filter by NULL sentiment).
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from sqlalchemy import text

from app.database import async_session


async def backfill_signals(*, dry_run: bool, limit: int) -> dict:
    counts = {"total": 0, "classified": 0, "skipped_no_body": 0, "failed": 0}
    from app.services.reply_classifier import classify_reply

    async with async_session() as db:
        rows = (await db.execute(text("""
            SELECT s.id, s.raw_data_json
            FROM signals s
            JOIN signal_types st ON st.id = s.signal_type_id
            WHERE st.code IN ('email_reply', 'sms_reply')
              AND (
                s.raw_data_json IS NULL
                OR NOT (s.raw_data_json ? 'sentiment')
              )
            ORDER BY s.observed_at DESC
            LIMIT :lim
        """), {"lim": limit})).fetchall()

    print(f"  found {len(rows)} unclassified signal(s)")

    for r in rows:
        counts["total"] += 1
        raw = r.raw_data_json or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        body = (raw.get("body_text") or raw.get("preview") or raw.get("body") or "").strip()
        subject = raw.get("subject") or ""
        if not body:
            counts["skipped_no_body"] += 1
            continue

        if dry_run:
            counts["classified"] += 1
            continue

        try:
            result = await classify_reply(body, subject)
        except Exception as e:
            print(f"  signal {r.id}: classify failed: {e}")
            counts["failed"] += 1
            continue

        if not result:
            counts["failed"] += 1
            continue

        sentiment = result.get("sentiment")
        summary = (result.get("summary") or "").strip()[:200]
        if not sentiment:
            counts["failed"] += 1
            continue

        async with async_session() as db:
            await db.execute(text("""
                UPDATE signals
                SET raw_data_json = COALESCE(raw_data_json, '{}'::jsonb)
                                    || jsonb_build_object(
                                        'sentiment',         :sentiment,
                                        'reply_sentiment',   :sentiment,
                                        'summary',           :summary
                                    )
                WHERE id = :id
            """), {
                "id": r.id,
                "sentiment": sentiment,
                "summary": summary,
            })
            await db.commit()
        counts["classified"] += 1
        if counts["classified"] % 25 == 0:
            print(f"  ... {counts['classified']}/{counts['total']} classified")

    return counts


async def backfill_activities(*, dry_run: bool, limit: int) -> dict:
    counts = {"total": 0, "classified": 0, "skipped_no_body": 0, "failed": 0}
    from app.services.reply_classifier import classify_reply

    async with async_session() as db:
        rows = (await db.execute(text("""
            SELECT id, contact_id, activity_type, content, metadata_json
            FROM activities
            WHERE activity_type IN ('email_replied', 'sms_inbound')
              AND reply_sentiment IS NULL
            ORDER BY created_at DESC
            LIMIT :lim
        """), {"lim": limit})).fetchall()

    print(f"  found {len(rows)} unclassified activity row(s)")

    for r in rows:
        counts["total"] += 1
        # Body lives in metadata_json.body_text or the content preview
        meta = r.metadata_json or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        body = (meta.get("body_text") or meta.get("preview") or r.content or "").strip()
        subject = meta.get("subject") or ""
        if not body:
            counts["skipped_no_body"] += 1
            continue

        if dry_run:
            counts["classified"] += 1
            continue

        try:
            result = await classify_reply(body, subject)
        except Exception as e:
            print(f"  activity {r.id}: classify failed: {e}")
            counts["failed"] += 1
            continue

        if not result:
            counts["failed"] += 1
            continue

        sentiment = result.get("sentiment")
        summary = (result.get("summary") or "").strip()[:200]
        if not sentiment:
            counts["failed"] += 1
            continue

        async with async_session() as db:
            await db.execute(text("""
                UPDATE activities
                SET reply_sentiment = :sentiment,
                    reply_sentiment_summary = :summary
                WHERE id = :id
            """), {
                "id": r.id, "sentiment": sentiment, "summary": summary,
            })
            await db.commit()
        counts["classified"] += 1
        if counts["classified"] % 25 == 0:
            print(f"  ... {counts['classified']}/{counts['total']} classified")

    return counts


async def main(dry_run: bool, limit: int):
    print(f"=== backfill_reply_classifications ===")
    print(f"  dry_run={dry_run} limit={limit}")
    print()
    print("Phase 1: signals (new engine)")
    sig_counts = await backfill_signals(dry_run=dry_run, limit=limit)
    print(f"  result: {sig_counts}")
    print()
    print("Phase 2: activities (legacy)")
    act_counts = await backfill_activities(dry_run=dry_run, limit=limit)
    print(f"  result: {act_counts}")
    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Count what would be classified, don't call the AI or write")
    parser.add_argument("--limit", type=int, default=2000,
                        help="Max rows per phase per run (default 2000)")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(dry_run=args.dry_run, limit=args.limit)))
