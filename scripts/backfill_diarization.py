"""Re-transcribe every Activity that has a recording_url but no
persisted diarization data. One-shot — run once after the migration
to fill in the new diarized_segments_json + talk_ratio_json columns.

Reuses the existing transcription pipeline so we get the same code
path as new calls. Skips activities that already have diarization
persisted (so it's safe to re-run)."""
from __future__ import annotations
import asyncio
import logging
from sqlalchemy import select

from app.database import async_session
from app.models import Activity

log = logging.getLogger("bmp.backfill")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def main() -> None:
    # We bypass the _run_pipeline early-exit on already-transcribed
    # activities by null-ing the transcript before re-running. Cleaner
    # than passing a force flag through the whole pipeline.
    import json
    from app.services.call_transcription import _run_pipeline as transcribe_activity

    async with async_session() as db:
        rows = (await db.execute(
            select(Activity).where(
                Activity.activity_type == "call",
                Activity.recording_url.isnot(None),
                Activity.diarized_segments_json.is_(None),
            ).order_by(Activity.id.desc())
        )).scalars().all()
        ids = [a.id for a in rows]
        # Null transcripts so the pipeline re-runs for these activities
        for a in rows:
            a.transcript = None
        await db.commit()

    log.info(f"Found {len(ids)} call activities with recordings but no diarization persisted")
    for aid in ids:
        try:
            log.info(f"  → re-transcribing activity {aid}…")
            await transcribe_activity(aid)
        except Exception as e:
            log.exception(f"    backfill failed for activity {aid}: {e}")

    log.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
