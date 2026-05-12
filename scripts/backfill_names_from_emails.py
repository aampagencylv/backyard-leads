"""Backfill first/last names from email addresses for contacts with no name."""
import asyncio, re
from sqlalchemy import select, or_
from app.database import async_session
from app.models import Contact

GENERIC_PREFIXES = frozenset({
    "info", "hello", "contact", "admin", "support", "sales", "office",
    "billing", "service", "team", "help", "marketing", "construction",
    "accounting", "hr", "jobs", "careers", "general", "mail", "enquiries",
    "inquiries", "noreply", "no-reply", "notifications", "ops",
})

def infer_name(email):
    if not email or "@" not in email:
        return None, None
    local = email.split("@")[0].lower().strip()
    if local in GENERIC_PREFIXES:
        return None, None
    parts = re.split(r'[._\-]', local)
    parts = [p for p in parts if p and len(p) > 1]
    parts = [p for p in parts if p not in GENERIC_PREFIXES]
    if not parts:
        return None, None
    first = parts[0].capitalize()
    last = parts[1].capitalize() if len(parts) > 1 else ""
    if not first.isalpha():
        return None, None
    return first, last

async def main():
    async with async_session() as db:
        nameless = (await db.execute(
            select(Contact).where(
                Contact.email.isnot(None),
                Contact.email != "",
                or_(
                    Contact.first_name.is_(None),
                    Contact.first_name == "",
                ),
            )
        )).scalars().all()

        print(f"Found {len(nameless)} contacts with no name")
        updated = 0
        for c in nameless:
            first, last = infer_name(c.email)
            if first:
                c.first_name = first
                if last:
                    c.last_name = last
                updated += 1
                print(f"  {c.email} → {first} {last}")

        await db.commit()
        print(f"\nUpdated {updated} contacts")

asyncio.run(main())
