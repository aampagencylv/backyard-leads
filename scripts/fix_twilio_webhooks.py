"""Configure inbound voice + SMS webhooks on all assigned Twilio numbers."""
import asyncio
from app.database import async_session
from app.runtime_config import get_twilio_credentials
from app.services.twilio_voice import list_owned_numbers, configure_inbound_voice_url
from app.models import User
from app.config import settings
from sqlalchemy import select

async def main():
    async with async_session() as db:
        creds = await get_twilio_credentials(db)
        owned = await list_owned_numbers(creds)

        # Get all users with assigned Twilio numbers
        users = (await db.execute(
            select(User).where(User.twilio_phone_number.isnot(None))
        )).scalars().all()

        assigned_phones = {u.twilio_phone_number: u for u in users}
        public = settings.public_url.rstrip('/')

        for n in owned:
            user = assigned_phones.get(n.phone_number)
            label = f"{n.phone_number} ({user.full_name})" if user else f"{n.phone_number} (unassigned)"

            if not user:
                # Clear webhooks on unassigned numbers so they don't ring our system
                try:
                    import httpx
                    clear_payload = {"VoiceUrl": "", "VoiceMethod": "POST", "StatusCallback": "", "SmsUrl": ""}
                    async with httpx.AsyncClient(timeout=15) as client:
                        await client.post(
                            f"https://api.twilio.com/2010-04-01/Accounts/{creds.account_sid}/IncomingPhoneNumbers/{n.sid}.json",
                            data=clear_payload,
                            auth=(creds.account_sid, creds.auth_token),
                        )
                    print(f"  ⊘ {label} — webhooks cleared (unassigned)")
                except Exception as e:
                    print(f"  ✗ {label} — clear FAILED: {e}")
                continue

            try:
                await configure_inbound_voice_url(
                    creds, n.sid,
                    voice_url=f"{public}/api/twilio/voice/inbound",
                    status_callback=f"{public}/api/twilio/voice/status",
                )
                print(f"  ✓ {label} — voice webhook set")
            except Exception as e:
                print(f"  ✗ {label} — voice webhook FAILED: {e}")

            # SMS webhook — set via raw API since there's no helper function
            try:
                import httpx
                sms_payload = {"SmsUrl": f"{public}/api/twilio/sms/inbound", "SmsMethod": "POST"}
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.post(
                        f"https://api.twilio.com/2010-04-01/Accounts/{creds.account_sid}/IncomingPhoneNumbers/{n.sid}.json",
                        data=sms_payload,
                        auth=(creds.account_sid, creds.auth_token),
                    )
                if r.status_code == 200:
                    print(f"  ✓ {label} — SMS webhook set")
                else:
                    print(f"  ✗ {label} — SMS webhook {r.status_code}: {r.text[:100]}")
            except Exception as e:
                print(f"  ✗ {label} — SMS webhook FAILED: {e}")

asyncio.run(main())
