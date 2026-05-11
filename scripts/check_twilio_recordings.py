"""Check Twilio API directly for any recordings on the account."""
import asyncio, httpx
from app.database import async_session
from app.runtime_config import get_twilio_credentials

async def main():
    async with async_session() as db:
        creds = await get_twilio_credentials(db)

    # Check recordings for the most recent call
    call_sid = "CA2f5d7cfe6a70cdcb0cf223f97e35894a"  # most recent call

    # List ALL recordings on the account (last 20)
    url = f"https://api.twilio.com/2010-04-01/Accounts/{creds.account_sid}/Recordings.json"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params={"PageSize": 20}, auth=(creds.account_sid, creds.auth_token))

    if r.status_code != 200:
        print(f"Failed: {r.status_code} {r.text[:200]}")
        return

    data = r.json()
    recordings = data.get("recordings", [])
    print(f"=== Total recordings on account: {len(recordings)} ===")
    for rec in recordings:
        print(f"  SID: {rec.get('sid')}")
        print(f"  Call SID: {rec.get('call_sid')}")
        print(f"  Duration: {rec.get('duration')}s")
        print(f"  Status: {rec.get('status')}")
        print(f"  Date: {rec.get('date_created')}")
        print(f"  URI: {rec.get('uri')}")
        print()

    if not recordings:
        print("NO RECORDINGS FOUND on the entire Twilio account.")
        print("This means Twilio is NOT recording calls at all.")
        print("Check: is recording enabled on the Twilio account? (Account > Settings > Voice)")

asyncio.run(main())
