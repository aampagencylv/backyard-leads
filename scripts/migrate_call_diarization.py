"""
Persist Deepgram diarization output on call Activity rows so the
dashboard can render the dual-channel CallRail-style waveform + the
agent-vs-customer talk-time percentages.

  activities:
    - diarized_segments_json (TEXT, nullable)
        Array of {speaker:int, start:float, end:float, text:str}.
    - talk_ratio_json (TEXT, nullable)
        {rep_words, prospect_words, rep_pct, prospect_pct}.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


COLUMNS = [
    ("diarized_segments_json", "TEXT"),
    ("talk_ratio_json", "TEXT"),
]


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(activities)"))).fetchall()}
        for name, ddl in COLUMNS:
            if name not in cols:
                await conn.execute(text(f"ALTER TABLE activities ADD COLUMN {name} {ddl}"))
                print(f"+ added activities.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
