"""Debug: detailed campaign status."""
import asyncio, json
from app.database import async_session
from app.models import Campaign, CampaignTarget, CampaignRun
from sqlalchemy import select, func
from datetime import datetime, timezone

async def main():
    async with async_session() as db:
        camps = (await db.execute(select(Campaign).order_by(Campaign.id.desc()))).scalars().all()
        for c in camps:
            bts = json.loads(c.business_types) if c.business_types else []
            locs = json.loads(c.locations) if c.locations else []
            tcount = (await db.execute(select(func.count(CampaignTarget.id)).where(CampaignTarget.campaign_id == c.id))).scalar() or 0
            print(f"Campaign #{c.id}: {c.name}")
            print(f"  status={c.status} mode={c.mode}")
            print(f"  prospects_today={c.prospects_today} max_per_day={c.max_prospects_per_day}")
            print(f"  found={c.total_prospects_found} qualified={c.total_qualified} sequences={c.total_sequences_created}")
            print(f"  loc_idx={c.current_location_index}/{len(locs)} bt_idx={c.current_business_type_index}/{len(bts)}")
            print(f"  last_run={c.last_run_at}")
            print(f"  last_daily_reset={c.last_daily_reset}")
            print(f"  targets={tcount}")
            print(f"  locations={locs}")
            print(f"  business_types={bts}")

            # Check if daily cap needs reset
            now = datetime.now(timezone.utc)
            if c.last_daily_reset:
                reset_date = c.last_daily_reset
                if reset_date.tzinfo is None:
                    from datetime import timezone as tz
                    reset_date = reset_date.replace(tzinfo=tz.utc)
                hours_since_reset = (now - reset_date).total_seconds() / 3600
                print(f"  hours_since_daily_reset={hours_since_reset:.1f}h")
                if hours_since_reset > 24:
                    print(f"  ** DAILY RESET OVERDUE — prospects_today should be 0 but is {c.prospects_today}")
            print()

        # Recent runs
        print("=== Last 5 campaign runs ===")
        try:
            runs = (await db.execute(
                select(CampaignRun).order_by(CampaignRun.id.desc()).limit(5)
            )).scalars().all()
            for r in runs:
                print(f"  Run #{r.id} campaign={r.campaign_id} found={r.companies_found} qualified={r.companies_qualified} seqs={r.sequences_created}")
                print(f"    created={r.created_at}")
                if hasattr(r, 'error_message') and r.error_message:
                    print(f"    error: {r.error_message[:100]}")
        except Exception as e:
            print(f"  Error: {e}")

asyncio.run(main())
