"""
Auto Pilot cron runner.
Finds all campaigns with status='running' and executes one batch for each
by calling the internal localhost API endpoint (no auth required).

Usage:
    cd /opt/backyard-leads
    source venv/bin/activate
    python -m scripts.run_autopilot
"""
import asyncio
import httpx
from datetime import datetime
from sqlalchemy import select, text


API_BASE = "http://127.0.0.1:8000"


async def get_running_campaign_ids():
    """Query the DB directly to get running campaign IDs (avoids auth)."""
    from app.database import async_session
    from app.models import Campaign

    async with async_session() as db:
        result = await db.execute(
            select(Campaign.id, Campaign.name, Campaign.prospects_today, Campaign.max_prospects_per_day,
                   Campaign.current_location_index, Campaign.locations)
            .where(Campaign.status == "running")
        )
        return result.all()


async def run():
    print(f"[{datetime.now().isoformat()}] Auto Pilot cron starting...")

    campaigns = await get_running_campaign_ids()
    if not campaigns:
        print("  No running campaigns. Exiting.")
        return

    print(f"  Found {len(campaigns)} running campaign(s)")

    async with httpx.AsyncClient(timeout=300) as client:
        for cid, name, today, max_day, loc_idx, locations_json in campaigns:
            import json
            locs = json.loads(locations_json) if locations_json else []
            print(f"\n  Campaign #{cid}: {name}")
            print(f"    Today: {today}/{max_day} | Location: {loc_idx}/{len(locs)}")

            if today >= max_day:
                print(f"    Daily cap reached. Skipping.")
                continue

            try:
                resp = await client.post(
                    f"{API_BASE}/api/campaigns/{cid}/run-batch-internal",
                    timeout=300,
                )

                if resp.status_code == 200:
                    result = resp.json()
                    status = result.get('status', 'ok')
                    print(f"    Status: {status}")
                    if 'searched' in result:
                        print(f"    Searched: {result.get('searched', 0)} | New: {result.get('new_companies', 0)} | Qualified: {result.get('qualified', 0)} | Sequences: {result.get('sequences_created', 0)}")
                else:
                    print(f"    ERROR: {resp.status_code} - {resp.text[:200]}")
            except Exception as e:
                print(f"    ERROR: {str(e)[:200]}")

    print(f"\n[{datetime.now().isoformat()}] Auto Pilot cron complete.")


if __name__ == "__main__":
    asyncio.run(run())
