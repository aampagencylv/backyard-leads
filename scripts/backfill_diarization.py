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
    from app.services.call_transcription import _run_pipeline as transcribe_activity

    async with async_session() as db:
        rows = (await db.execute(
            select(Activity).where(
                Activity.activity_type == "call",
                Activity.recording_url.isnot(None),
                Activity.diarized_segments_json.is_(None),
            ).order_by(Activity.id.desc())
        )).scalars().all()

    log.info(f"Found {len(rows)} call activities with recordings but no diarization persisted")
    for a in rows:
        try:
            log.info(f"  → re-transcribing activity {a.id} (recording {a.recording_url[-30:]}…)")
            # transcribe_activity opens its own session
            await transcribe_activity(a.id)
        except Exception as e:
            log.exception(f"    backfill failed for activity {a.id}: {e}")

    log.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
