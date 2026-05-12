"""Bulk regenerate sequences for all companies that have contacts with sequences.
Preserves sent steps, regenerates unsent ones with the full 13-step template
(including calls). Also ensures each company has an AI Findability Audit.

Safe to run multiple times — idempotent."""
import asyncio
import logging
from sqlalchemy import select, func
from app.database import async_session
from app.models import Company, Contact, GeneratedEmail, AuditReportModel
from app.services.sequence_engine import start_sequence_from_template

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("regen")


async def main():
    async with async_session() as db:
        # Find all contacts that have at least one generated email
        contact_ids_with_seq = (await db.execute(
            select(GeneratedEmail.contact_id).group_by(GeneratedEmail.contact_id)
        )).scalars().all()

        log.info(f"Found {len(contact_ids_with_seq)} contacts with sequences")

        regenerated = 0
        audits_created = 0
        skipped = 0

        for contact_id in contact_ids_with_seq:
            contact = (await db.execute(
                select(Contact).where(Contact.id == contact_id)
            )).scalar_one_or_none()
            if not contact:
                continue

            company = (await db.execute(
                select(Company).where(Company.id == contact.company_id)
            )).scalar_one_or_none()
            if not company:
                continue

            # Check how many unsent steps exist
            unsent_count = (await db.execute(
                select(func.count(GeneratedEmail.id)).where(
                    GeneratedEmail.contact_id == contact_id,
                    GeneratedEmail.sequence_label == "main",
                    GeneratedEmail.is_sent == False,
                    GeneratedEmail.skipped_at.is_(None),
                )
            )).scalar() or 0

            sent_count = (await db.execute(
                select(func.count(GeneratedEmail.id)).where(
                    GeneratedEmail.contact_id == contact_id,
                    GeneratedEmail.sequence_label == "main",
                    GeneratedEmail.is_sent == True,
                )
            )).scalar() or 0

            # Check if it already has call steps
            has_calls = (await db.execute(
                select(func.count(GeneratedEmail.id)).where(
                    GeneratedEmail.contact_id == contact_id,
                    GeneratedEmail.step_type == "call",
                )
            )).scalar() or 0

            if has_calls > 0:
                skipped += 1
                continue  # Already has the full template

            log.info(f"  {company.name} / {contact.full_name or contact.email}: {sent_count} sent, {unsent_count} unsent, {has_calls} calls → regenerating")

            # Delete unsent, non-skipped steps (preserves sent history)
            unsent = (await db.execute(
                select(GeneratedEmail).where(
                    GeneratedEmail.contact_id == contact_id,
                    GeneratedEmail.sequence_label == "main",
                    GeneratedEmail.is_sent == False,
                )
            )).scalars().all()
            for ge in unsent:
                await db.delete(ge)
            await db.flush()

            # Regenerate with the full 13-step template
            try:
                created = await start_sequence_from_template(
                    db, contact,
                    sequence_label="main",
                    pre_generate_emails=True,
                )
                regenerated += 1
                log.info(f"    → {created} steps created")
            except Exception as e:
                log.warning(f"    → FAILED: {e}")

            # Ensure audit exists
            try:
                existing_audit = (await db.execute(
                    select(AuditReportModel).where(AuditReportModel.company_id == company.id)
                )).scalar_one_or_none()
                if not existing_audit and company.website:
                    from app.services.audit_report import ensure_audit_for_company
                    url = await ensure_audit_for_company(db, company)
                    if url:
                        audits_created += 1
                        log.info(f"    → Audit created: {url}")
            except Exception as e:
                log.warning(f"    → Audit failed: {e}")

            await db.commit()

        log.info(f"\nDone: {regenerated} regenerated, {skipped} already had calls, {audits_created} audits created")

asyncio.run(main())
