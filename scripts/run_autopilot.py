"""
Auto Pilot cron runner.
Finds all campaigns with status='running' and executes one batch for each.
Designed to be called by cron every N minutes (e.g., every 30 min during business hours).

Usage:
    cd /opt/backyard-leads
    source venv/bin/activate
    python -m scripts.run_autopilot
"""
import asyncio
import httpx
from datetime import datetime


API_BASE = "http://127.0.0.1:8000"


async def run():
    print(f"[{datetime.now().isoformat()}] Auto Pilot cron starting...")

    async with httpx.AsyncClient(timeout=300) as client:
        # Get all running campaigns
        campaigns_resp = await client.get(f"{API_BASE}/api/campaigns/")
        if campaigns_resp.status_code != 200:
            print(f"  ERROR: Could not list campaigns: {campaigns_resp.status_code}")
            return

        campaigns = campaigns_resp.json()
        running = [c for c in campaigns if c["status"] == "running"]

        if not running:
            print("  No running campaigns. Exiting.")
            return

        print(f"  Found {len(running)} running campaign(s)")

        for campaign in running:
            cid = campaign["id"]
            name = campaign["name"]
            print(f"\n  Campaign #{cid}: {name}")
            print(f"    Status: {campaign['status']} | Today: {campaign['prospects_today']}/{campaign['max_prospects_per_day']}")
            print(f"    Progress: location {campaign['current_location_index']}/{len(campaign['locations'])}")

            # Need auth — use the campaign creator's token
            # For cron, we'll use a service endpoint that doesn't require auth
            # The run-batch endpoint requires auth, so we'll call it internally
            try:
                # Run batch via internal API (no auth needed for localhost)
                batch_resp = await client.post(
                    f"{API_BASE}/api/campaigns/{cid}/run-batch-internal",
                    timeout=300,
                )

                if batch_resp.status_code == 200:
                    result = batch_resp.json()
                    print(f"    Result: {result.get('status', 'ok')}")
                    print(f"    Searched: {result.get('searched', 0)} | New: {result.get('new_companies', 0)} | Qualified: {result.get('qualified', 0)} | Sequences: {result.get('sequences_created', 0)}")
                else:
                    print(f"    ERROR: {batch_resp.status_code} - {batch_resp.text[:200]}")
            except Exception as e:
                print(f"    ERROR: {str(e)[:200]}")

    print(f"\n[{datetime.now().isoformat()}] Auto Pilot cron complete.")


if __name__ == "__main__":
    asyncio.run(run())
