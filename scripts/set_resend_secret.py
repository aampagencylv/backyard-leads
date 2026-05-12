"""Set the Resend webhook signing secret in the database."""
import asyncio
from app.database import async_session
from app.models import RuntimeConfig
from sqlalchemy import select

SECRET = "whsec_lj7fIhJbEC5Uv847Fl3SzY9c8Q5IM49v"

async def main():
    async with async_session() as db:
        rc = (await db.execute(select(RuntimeConfig).where(RuntimeConfig.id == 1))).scalar_one_or_none()
        if rc:
            rc.resend_webhook_secret = SECRET
            await db.commit()
            print(f"Updated resend_webhook_secret: {SECRET[:10]}...")
        else:
            print("No runtime_config row found — creating one")
            rc = RuntimeConfig(id=1, resend_webhook_secret=SECRET)
            db.add(rc)
            await db.commit()
            print(f"Created runtime_config with secret: {SECRET[:10]}...")

asyncio.run(main())
