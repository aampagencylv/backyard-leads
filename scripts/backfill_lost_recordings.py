"""One-time recovery for the recording-webhook race (2026-06-10 incident).

~65% of call Activities had no recording_url while the audio sat in Twilio:
the recording-complete webhook fired before log_call created the Activity
row, and the old handler dropped the URL. This script:

  1. Sweeps ALL call Activities (default: last 60 days) missing a
     recording_url and attaches recordings straight from the Twilio API
     (own CallSid first, then the parent leg).
  2. Transcribes every recording that has no transcript yet, with bounded
     concurrency so Deepgram/Twilio aren't hammered.

Idempotent — attached recordings drop out of the query; transcription
skips rows that already have a transcript. Safe to re-run.

Usage:
    python -m scripts.backfill_lost_recordings [--days 60] [--no-transcribe]
"""
import argparse
import asyncio
import sys

from sqlalchemy import select

from app.database import async_session
from app.models import Activity
from app.services.call_reconciliation import backfill_missing_recordings


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--no-transcribe", action="store_true")
    args = ap.parse_args()

    # Phase 1: attach recordings. Loop until a sweep finds nothing left to
    # check (each sid is attempted at most 3 times via metadata bookkeeping,
    # so unrecorded calls age out of the loop).
    total_attached = 0
    while True:
        async with async_session() as db:
            counters = await backfill_missing_recordings(
                db, hours=args.days * 24, limit=100,
            )
        total_attached += counters["attached"]
        print(f"sweep: {counters} (total attached so far: {total_attached})")
        if counters["checked"] == 0:
            break
    print(f"=== phase 1 done: {total_attached} recordings recovered ===")

    if args.no_transcribe:
        return 0

    # Phase 2: transcribe everything that now has a recording but no
    # transcript. transcribe_and_summarize_in_background is idempotent
    # (skips rows that already have a transcript).
    async with async_session() as db:
        ids = (await db.execute(
            select(Activity.id)
            .where(
                Activity.activity_type == "call",
                Activity.recording_url.is_not(None),
                Activity.transcript.is_(None),
            )
            .order_by(Activity.id.desc())
        )).scalars().all()
    print(f"=== phase 2: transcribing {len(ids)} recordings ===")

    from app.services.call_transcription import transcribe_and_summarize_in_background
    sem = asyncio.Semaphore(3)
    done = 0

    async def _one(aid: int) -> None:
        nonlocal done
        async with sem:
            try:
                await transcribe_and_summarize_in_background(aid)
            except Exception as e:
                print(f"  transcription failed for activity {aid}: {e}")
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(ids)} transcribed")

    await asyncio.gather(*(_one(a) for a in ids))
    print(f"=== phase 2 done: {done}/{len(ids)} processed ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
