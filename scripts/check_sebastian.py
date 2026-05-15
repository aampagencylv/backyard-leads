"""Check Sebastian's Twilio config."""
import asyncio, httpx
from sqlalchemy import select
from app.database import async_session
from app.models import User
from app.runtime_config import get_twilio_credentials

async def main():
    async with async_session() as db:
        u = (await db.execute(select(User).where(User.first_name == "Sebastian"))).scalar_one_or_none()
        if not u:
            print("Sebastian not found")
            return
        print(f"Name: {u.first_name} {u.last_name}")
        print(f"Identity: {u.twilio_identity}")
        print(f"Number: {u.twilio_phone_number}")
        print(f"Dial mode: {u.dial_mode}")
        print(f"Available: {u.is_available_for_calls}")
        print(f"Personal phone: {u.personal_phone_number}")

        # Check his recent calls in Twilio
        creds = await get_twilio_credentials(db)
        auth = (creds.account_sid, creds.auth_token)
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{creds.account_sid}/Calls.json",
                params={"From": u.twilio_phone_number, "PageSize": 5},
                auth=auth,
            )
            calls = r.json().get("calls", [])
            print(f"\nRecent calls FROM his number:")
            for c in calls:
                print(f"  {c['from_formatted']} -> {c['to_formatted']} | {c['status']} | {c['duration']}s | {c['date_created']}")

            # Also check calls from his client identity
            r2 = await client.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{creds.account_sid}/Calls.json",
                params={"From": f"client:{u.twilio_identity}", "PageSize": 5},
                auth=auth,
            )
            calls2 = r2.json().get("calls", [])
            print(f"\nRecent calls FROM his client identity:")
            for c in calls2:
                print(f"  {c['status']} | {c['duration']}s | {c['date_created']} | error={c.get('error_code') or 'none'}")

asyncio.run(main())
