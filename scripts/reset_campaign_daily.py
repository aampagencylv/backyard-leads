"""Reset daily prospect counter on a campaign so it can keep running today."""
import asyncio
from app.database import async_session
from app.models import Campaign
from sqlalchemy import select

async def main():
    async with async_session() as db:
        c = (await db.execute(select(Campaign).where(Campaign.id == 1))).scalar_one_or_none()
        if c:
            old = c.prospects_today
            c.prospects_today = 0
            await db.commit()
            print(f"Campaign #{c.id}: reset prospects_today from {old} to 0")
        else:
            print("Campaign #1 not found")

asyncio.run(main())
