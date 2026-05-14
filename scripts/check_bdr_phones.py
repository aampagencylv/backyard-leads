"""Check BDR phone configuration for inbound call routing."""
import asyncio
from sqlalchemy import select
from app.database import async_session
from app.models import User

async def main():
    async with async_session() as db:
        users = (await db.execute(select(User).where(User.is_active == True))).scalars().all()
        for u in users:
            print(f"{u.first_name} {u.last_name}:")
            print(f"  twilio_number: {u.twilio_phone_number or 'NONE'}")
            print(f"  personal_phone: {u.personal_phone_number or 'NONE'}")
            print(f"  dial_mode: {u.dial_mode}")
            print(f"  identity: {u.twilio_identity or 'NONE'}")
            if not u.personal_phone_number:
                print(f"  WARNING: No personal phone — inbound calls only ring in browser!")
            print()

asyncio.run(main())
