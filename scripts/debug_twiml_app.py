"""Debug: check the TwiML App configuration in Twilio."""
import asyncio, httpx
from app.database import async_session
from app.runtime_config import get_twilio_credentials

async def main():
    async with async_session() as db:
        creds = await get_twilio_credentials(db)
        print(f"TwiML App SID: {creds.twiml_app_sid}")

        if not creds.twiml_app_sid:
            print("No TwiML App configured!")
            return

        url = f"https://api.twilio.com/2010-04-01/Accounts/{creds.account_sid}/Applications/{creds.twiml_app_sid}.json"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, auth=(creds.account_sid, creds.auth_token))

        if r.status_code != 200:
            print(f"Failed to fetch app: {r.status_code} {r.text[:200]}")
            return

        data = r.json()
        print(f"App Name: {data.get('friendly_name')}")
        print(f"Voice URL: {data.get('voice_url')}")
        print(f"Voice Method: {data.get('voice_method')}")
        print(f"Status Callback: {data.get('status_callback')}")
        print(f"Status Method: {data.get('status_callback_method')}")

        voice_url = data.get('voice_url', '')
        if 'prospector.backyardmarketingpros.com' in voice_url:
            print("\nOK: Voice URL points to our server")
        else:
            print(f"\nPROBLEM: Voice URL does NOT point to our server!")
            print(f"Expected: https://prospector.backyardmarketingpros.com/api/twilio/voice/twiml")

asyncio.run(main())
