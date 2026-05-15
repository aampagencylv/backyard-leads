"""Switch Sebastian from bridge to browser mode."""
import asyncio
from sqlalchemy import select
from app.database import async_session
from app.models import User

async def main():
    async with async_session() as db:
        u = (await db.execute(select(User).where(User.first_name == "Sebastian"))).scalar_one_or_none()
        if u:
            print(f"Before: dial_mode={u.dial_mode} personal_phone={u.personal_phone_number}")
            u.dial_mode = "browser"
            await db.commit()
            print(f"After: dial_mode=browser — calls will go through his browser now")

asyncio.run(main())
