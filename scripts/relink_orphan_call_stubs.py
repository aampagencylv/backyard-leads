"""Re-link orphaned reconciliation call stubs to their companies/contacts.

Every reconciliation stub ever created (330+ as of 2026-06-10) has
company_id NULL: the phone match compared Twilio's E.164 number against
the CRM's pretty-formatted phones ("(954) 327-3686") and never hit. The
stubs exist but appear on no company timeline — reps experienced this as
"the CRM isn't logging my calls".

This script extracts the dialed number from each orphan stub's content,
matches contacts/companies by last-10 digits (same logic the fixed
reconciler now uses), and sets contact_id/company_id. Idempotent.

Usage: python -m scripts.relink_orphan_call_stubs [--dry-run]
"""
import argparse
import asyncio
import re
import sys

from sqlalchemy import text

from app.database import async_session


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    async with async_session() as db:
        rows = (await db.execute(text("""
            SELECT id, content FROM activities
            WHERE activity_type = 'call'
              AND metadata_json LIKE '%reconciliation%'
              AND company_id IS NULL
            ORDER BY id
        """))).fetchall()
        print(f"{len(rows)} orphan stubs to examine")

        linked_contact = linked_company = unmatched = 0
        for r in rows:
            m = re.search(r"\+([0-9]{10,15})", r.content or "")
            if not m:
                unmatched += 1
                continue
            digits = m.group(1)[-10:]
            ct = (await db.execute(text("""
                SELECT id, company_id FROM contacts
                WHERE phone IS NOT NULL AND phone != ''
                  AND RIGHT(regexp_replace(phone, '[^0-9]', '', 'g'), 10) = :d
                ORDER BY is_primary DESC, id LIMIT 1
            """), {"d": digits})).first()
            if ct:
                if not args.dry_run:
                    await db.execute(text("""
                        UPDATE activities SET contact_id = :ct, company_id = :co
                        WHERE id = :id
                    """), {"ct": int(ct.id), "co": int(ct.company_id), "id": r.id})
                linked_contact += 1
                continue
            co = (await db.execute(text("""
                SELECT id FROM companies
                WHERE phone IS NOT NULL AND phone != ''
                  AND RIGHT(regexp_replace(phone, '[^0-9]', '', 'g'), 10) = :d
                ORDER BY id LIMIT 1
            """), {"d": digits})).first()
            if co:
                if not args.dry_run:
                    await db.execute(text("""
                        UPDATE activities SET company_id = :co WHERE id = :id
                    """), {"co": int(co.id), "id": r.id})
                linked_company += 1
            else:
                unmatched += 1

        if not args.dry_run:
            await db.commit()
        print(f"linked to contact: {linked_contact}, to company only: {linked_company}, "
              f"no match (number not in CRM): {unmatched}"
              f"{' [DRY RUN — nothing written]' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
