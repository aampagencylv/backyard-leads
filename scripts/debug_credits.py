"""Debug: check credit meter accuracy vs actual usage."""
import asyncio
from sqlalchemy import select, func, text
from app.database import async_session
from app.models import Company, Contact

async def main():
    async with async_session() as db:
        companies = (await db.execute(select(func.count(Company.id)))).scalar()
        enriched = (await db.execute(select(func.count(Company.id)).where(Company.enriched == True))).scalar()
        contacts = (await db.execute(select(func.count(Contact.id)))).scalar()
        print(f"Companies: {companies} (enriched: {enriched})")
        print(f"Contacts: {contacts}")

        # Credit ledger breakdown
        print("\n=== Credit Ledger ===")
        try:
            rows = (await db.execute(text(
                "SELECT action_type, COUNT(*), COALESCE(SUM(raw_cost_usd), 0) "
                "FROM credit_ledger GROUP BY action_type ORDER BY COUNT(*) DESC"
            ))).fetchall()
            for r in rows:
                print(f"  {r[0]}: count={r[1]} cost=${float(r[2]):.2f}")
        except Exception as e:
            print(f"  Error: {e}")

        # Check what the enrichment waterfall actually did
        print("\n=== Enrichment sources on contacts ===")
        try:
            # Count contacts by whether they have linkedin, phone, etc
            with_email = (await db.execute(select(func.count(Contact.id)).where(Contact.email.isnot(None), Contact.email != ""))).scalar()
            with_phone = (await db.execute(select(func.count(Contact.id)).where(Contact.phone.isnot(None), Contact.phone != ""))).scalar()
            with_linkedin = (await db.execute(select(func.count(Contact.id)).where(Contact.linkedin_url.isnot(None), Contact.linkedin_url != ""))).scalar()
            print(f"  With email: {with_email}")
            print(f"  With phone: {with_phone}")
            print(f"  With LinkedIn: {with_linkedin}")
        except Exception as e:
            print(f"  Error: {e}")

        # Check if metering is happening on enrichment
        print("\n=== Metering gaps ===")
        print(f"  Enriched companies: {enriched}")
        try:
            metered_enrichments = (await db.execute(text(
                "SELECT COUNT(*) FROM credit_ledger WHERE action_type LIKE '%enrich%'"
            ))).scalar()
            print(f"  Metered enrichments: {metered_enrichments}")
            if enriched > 0 and metered_enrichments < enriched:
                print(f"  GAP: {enriched - metered_enrichments} enrichments not metered!")
        except Exception as e:
            print(f"  Error: {e}")

asyncio.run(main())
