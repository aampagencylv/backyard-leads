"""Check Twilio number webhook URLs — verify inbound routing is configured."""
import asyncio
from app.database import async_session
from app.runtime_config import get_twilio_credentials
from app.services.twilio_voice import list_owned_numbers

async def main():
    async with async_session() as db:
        c = await get_twilio_credentials(db)
        nums = await list_owned_numbers(c)
        for n in nums:
            print(f"{n.phone_number}")
            print(f"  voice_url:  {getattr(n, 'voice_url', 'N/A')}")
            print(f"  status_cb:  {getattr(n, 'status_callback', 'N/A')}")
            print(f"  sms_url:    {getattr(n, 'sms_url', 'N/A')}")
            print(f"  sid:        {n.sid}")

asyncio.run(main())
