"""One-shot: run the same Twilio call reconciliation the background
loop does, but over a wider window. Useful for recovering missing call
Activity rows after a code change or after noticing a gap in the team
dashboard's per-rep call counts.

  python -m scripts.backfill_call_reconciliation              # last 24h
  python -m scripts.backfill_call_reconciliation --hours 72   # last 3d
"""
from __future__ import annotations
import asyncio
import sys
from app.database import async_session
from app.services.call_reconciliation import reconcile_calls


async def main(hours: int):
    async with async_session() as db:
        counters = await reconcile_calls(db, hours=hours)
    print("Reconciliation results:")
    for k, v in counters.items():
        print(f"  {k:<25} {v}")


if __name__ == "__main__":
    hours = 24
    for i, a in enumerate(sys.argv):
        if a == "--hours" and i + 1 < len(sys.argv):
            hours = int(sys.argv[i + 1])
    asyncio.run(main(hours))
