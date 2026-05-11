"""Configure inbound voice + SMS webhooks on all assigned Twilio numbers."""
import asyncio
from app.database import async_session
from app.runtime_config import get_twilio_credentials
from app.services.twilio_voice import list_owned_numbers, configure_inbound_voice_url, configure_sms_webhook
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

            try:
                await configure_inbound_voice_url(
                    creds, n.sid,
                    voice_url=f"{public}/api/twilio/voice/inbound",
                    status_callback=f"{public}/api/twilio/voice/status",
                )
                print(f"  ✓ {label} — voice webhook set")
            except Exception as e:
                print(f"  ✗ {label} — voice webhook FAILED: {e}")

            try:
                await configure_sms_webhook(
                    creds, n.sid,
                    sms_url=f"{public}/api/twilio/sms/inbound",
                )
                print(f"  ✓ {label} — SMS webhook set")
            except Exception as e:
                print(f"  ✗ {label} — SMS webhook FAILED: {e}")

asyncio.run(main())
