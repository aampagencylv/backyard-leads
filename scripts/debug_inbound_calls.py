"""Debug inbound call routing — check everything end to end."""
import asyncio
import httpx
from app.database import async_session
from app.runtime_config import get_twilio_credentials
from app.models import User
from sqlalchemy import select

TWILIO_BASE = "https://api.twilio.com/2010-04-01"

async def main():
    async with async_session() as db:
        creds = await get_twilio_credentials(db)
        auth = (creds.account_sid, creds.auth_token)

        users = (await db.execute(select(User).where(User.is_active == True, User.twilio_phone_number.isnot(None)))).scalars().all()

        async with httpx.AsyncClient(timeout=15) as client:
            # Check each number's webhook config
            print("=== Twilio Number Webhook Configuration ===")
            r = await client.get(f"{TWILIO_BASE}/Accounts/{creds.account_sid}/IncomingPhoneNumbers.json?PageSize=20", auth=auth)
            numbers = r.json().get("incoming_phone_numbers", [])

            for n in numbers:
                phone = n.get("phone_number")
                voice_url = n.get("voice_url") or "NOT SET"
                voice_method = n.get("voice_method") or "?"
                status_cb = n.get("status_callback") or "NOT SET"
                user = next((u for u in users if u.twilio_phone_number == phone), None)
                assigned = f"{user.first_name} {user.last_name} (identity={user.twilio_identity})" if user else "UNASSIGNED"

                print(f"\n  {phone} → {assigned}")
                print(f"    voice_url: {voice_url}")
                print(f"    voice_method: {voice_method}")
                print(f"    status_callback: {status_cb}")

                if "voice/inbound" not in voice_url:
                    print(f"    ⚠️  PROBLEM: voice_url does not point to /api/twilio/voice/inbound!")

            # Check TwiML App config
            print(f"\n=== TwiML App Configuration ===")
            print(f"  TwiML App SID: {creds.twiml_app_sid}")
            if creds.twiml_app_sid:
                r2 = await client.get(f"{TWILIO_BASE}/Accounts/{creds.account_sid}/Applications/{creds.twiml_app_sid}.json", auth=auth)
                if r2.status_code == 200:
                    app = r2.json()
                    print(f"  App Name: {app.get('friendly_name')}")
                    print(f"  Voice URL: {app.get('voice_url')}")
                    print(f"  Voice Method: {app.get('voice_method')}")
                else:
                    print(f"  ERROR fetching app: {r2.status_code}")

            # Check if any calls are currently in progress
            print(f"\n=== Recent Calls (last 5) ===")
            r3 = await client.get(f"{TWILIO_BASE}/Accounts/{creds.account_sid}/Calls.json?PageSize=5", auth=auth)
            calls = r3.json().get("calls", [])
            for c in calls:
                direction = c.get("direction")
                status = c.get("status")
                from_num = c.get("from_formatted") or c.get("from")
                to_num = c.get("to_formatted") or c.get("to")
                duration = c.get("duration")
                print(f"  {direction} {from_num} → {to_num} | status={status} duration={duration}s")

asyncio.run(main())
