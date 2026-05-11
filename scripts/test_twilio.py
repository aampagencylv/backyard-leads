"""Quick test: verify Twilio credentials and list owned numbers."""
import asyncio
from app.database import async_session
from app.runtime_config import get_twilio_credentials
from app.services.twilio_voice import list_owned_numbers

async def main():
    async with async_session() as db:
        creds = await get_twilio_credentials(db)
        print(f"SID: {creds.account_sid[:15]}...")
        print(f"Configured: {creds.is_minimally_configured}")
        try:
            nums = await list_owned_numbers(creds)
            print(f"Numbers found: {len(nums)}")
            for n in nums:
                print(f"  {n.phone_number} ({n.friendly_name})")
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")

asyncio.run(main())
