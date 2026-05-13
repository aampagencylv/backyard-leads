"""Backfill phone numbers on contacts that have none — uses company main line."""
import asyncio
from sqlalchemy import select, or_
from app.database import async_session
from app.models import Contact, Company

async def main():
    async with async_session() as db:
        # Find contacts with no phone
        no_phone = (await db.execute(
            select(Contact, Company.phone)
            .join(Company, Contact.company_id == Company.id)
            .where(
                or_(Contact.phone.is_(None), Contact.phone == ""),
                Company.phone.isnot(None),
                Company.phone != "",
            )
        )).all()

        print(f"Found {len(no_phone)} contacts with no phone (company has one)")
        updated = 0
        for contact, company_phone in no_phone:
            contact.phone = company_phone
            updated += 1

        await db.commit()
        print(f"Updated {updated} contacts with company main line")

asyncio.run(main())
