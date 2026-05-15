"""Check Twilio account status and recent call outcomes."""
import asyncio
import httpx
from app.database import async_session
from app.runtime_config import get_twilio_credentials

async def main():
    async with async_session() as db:
        creds = await get_twilio_credentials(db)
        auth = (creds.account_sid, creds.auth_token)

        async with httpx.AsyncClient(timeout=15) as client:
            # Account status
            r = await client.get(f"https://api.twilio.com/2010-04-01/Accounts/{creds.account_sid}.json", auth=auth)
            if r.status_code == 200:
                d = r.json()
                print(f"Account: {d.get('friendly_name')}")
                print(f"Status: {d.get('status')}")
                print(f"Type: {d.get('type')}")
            else:
                print(f"Account check failed: {r.status_code}")

            # Balance
            r2 = await client.get(f"https://api.twilio.com/2010-04-01/Accounts/{creds.account_sid}/Balance.json", auth=auth)
            if r2.status_code == 200:
                b = r2.json()
                print(f"Balance: ${b.get('balance')} {b.get('currency')}")

            # Last 5 calls
            r3 = await client.get(f"https://api.twilio.com/2010-04-01/Accounts/{creds.account_sid}/Calls.json?PageSize=5", auth=auth)
            calls = r3.json().get("calls", [])
            print(f"\nLast 5 calls:")
            for c in calls:
                print(f"  {c.get('direction')} {c.get('from_formatted')} → {c.get('to_formatted')} | {c.get('status')} | {c.get('duration')}s | {c.get('date_created')}")

asyncio.run(main())
