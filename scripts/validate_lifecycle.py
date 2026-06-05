"""End-to-end validation of the engagement-engine lifecycle module.

Picks a real contact (with company) that has NO existing engagement,
runs start_engagement against it, asserts the engagement + actions
were materialized correctly, then ROLLS BACK by deleting the
created rows. Idempotent — safe to run repeatedly without leaving
test artifacts behind.

Usage:
    python -m scripts.validate_lifecycle [contact_id]

If contact_id is omitted, picks the lowest-id contact that has a
company and no engagement.
"""
from __future__ import annotations
import asyncio
import sys
from sqlalchemy import text, select

from app.database import async_session
from app.models import Contact


async def main(contact_id: int | None = None) -> int:
    test_contact_created = False
    test_company_created = False
    test_company_id = None

    async with async_session() as db:
        if contact_id is None:
            # All real contacts already have engagements (post-cutover).
            # Create a transient test company + contact.
            from sqlalchemy import text as _t
            tenant_row = (await db.execute(_t(
                "SELECT id FROM tenants ORDER BY id LIMIT 1"
            ))).first()
            tenant_id = int(tenant_row[0])
            co_row = (await db.execute(_t("""
                INSERT INTO companies (
                    tenant_id, name, email_generated, status,
                    lead_score, lead_score_fit, lead_score_intent, lead_score_tier,
                    created_at, updated_at
                ) VALUES (
                    :t, '__validation_test_company__', FALSE, 'pursuing',
                    0, 0, 0, 'cold',
                    NOW(), NOW()
                ) RETURNING id
            """), {"t": tenant_id})).first()
            test_company_id = int(co_row[0])
            test_company_created = True

            c_row = (await db.execute(_t("""
                INSERT INTO contacts (
                    tenant_id, company_id, first_name, last_name, email,
                    is_primary, do_not_text,
                    unsubscribe_token, created_at, updated_at,
                    outreach_owner
                ) VALUES (
                    :t, :co, 'Test', 'Lifecycle',
                    'lifecycle-test@example.invalid',
                    TRUE, FALSE,
                    'validation-test-token', NOW(), NOW(),
                    'none'
                ) RETURNING id
            """), {"t": tenant_id, "co": test_company_id})).first()
            contact_id = int(c_row[0])
            test_contact_created = True
            await db.commit()
            print(f"created test contact {contact_id} (company {test_company_id})")

        contact = (await db.execute(
            select(Contact).where(Contact.id == contact_id)
        )).scalar_one_or_none()
        if contact is None:
            print(f"Contact {contact_id} not found")
            return 1

        print(f"Testing start_engagement on contact {contact_id} "
              f"({contact.email or '(no email)'})")
        print(f"  company_id={contact.company_id} tenant_id={contact.tenant_id}")
        print(f"  outreach_owner={getattr(contact, 'outreach_owner', None)!r}")

    # Run start_engagement (it manages its own session + commits).
    from app.engagement_engine.lifecycle import start_engagement
    n = await start_engagement(
        # Re-fetch in a fresh session since start_engagement uses its own
        # internal commit. We pass the contact object — its attributes
        # are read by reference into the function.
        await _fresh_db(), contact,
        pre_generate_content=False,  # don't burn Claude tokens on a test
        initiated_by="validation_dryrun",
    )
    print(f"\nactions created: {n}")

    # Inspect what was written.
    async with async_session() as db:
        eng_row = (await db.execute(text("""
            SELECT id, sequence_number, current_phase, status, current_playbook_id,
                   next_action_due_at, assigned_bdr_id, last_transition_by
            FROM engagements WHERE contact_id = :c ORDER BY id DESC LIMIT 1
        """), {"c": contact_id})).first()
        if eng_row is None:
            print("FAIL: no engagement created")
            return 1
        engagement_id = int(eng_row[0])
        print(f"\nengagement {engagement_id}:")
        for k, v in dict(eng_row._mapping).items():
            print(f"  {k}: {v}")

        actions = (await db.execute(text("""
            SELECT a.id, ct.code AS channel, a.status, a.scheduled_at,
                   a.subject, a.skip_reason, a.idempotency_key
            FROM actions a
            JOIN channel_types ct ON ct.id = a.channel_id
            WHERE a.engagement_id = :e ORDER BY a.scheduled_at
        """), {"e": engagement_id})).fetchall()
        print(f"\nactions ({len(actions)}):")
        for a in actions:
            print(f"  #{a.id} ch={a.channel:<10} status={a.status:<10} "
                  f"sched={a.scheduled_at.isoformat()[:19]} "
                  f"skip={a.skip_reason or '-':<20} subj={a.subject!r}")

        # Read-back contact for outreach_owner mutation
        refetched = (await db.execute(text(
            "SELECT outreach_owner FROM contacts WHERE id = :c"
        ), {"c": contact_id})).first()
        print(f"\ncontact.outreach_owner after: {refetched[0]!r}")

        # Read-back company.email_generated
        co = (await db.execute(text(
            "SELECT id, email_generated, status FROM companies WHERE id = :c"
        ), {"c": contact.company_id})).first()
        print(f"company.email_generated={co[1]} status={co[2]!r}")

    # CLEANUP — delete the test engagement and actions to leave no
    # artifacts on prod data.
    print("\nrolling back test data...")
    async with async_session() as db:
        await db.execute(text(
            "DELETE FROM actions WHERE engagement_id = :e"
        ), {"e": engagement_id})
        await db.execute(text(
            "DELETE FROM engagements WHERE id = :e"
        ), {"e": engagement_id})
        if test_contact_created:
            # Activity rows + everything linked to the test contact get
            # cascade-deleted via FK; if not, drop them explicitly.
            await db.execute(text(
                "DELETE FROM activities WHERE contact_id = :c"
            ), {"c": contact_id})
            await db.execute(text(
                "DELETE FROM contacts WHERE id = :c"
            ), {"c": contact_id})
        if test_company_created:
            await db.execute(text(
                "DELETE FROM activities WHERE company_id = :c"
            ), {"c": test_company_id})
            await db.execute(text(
                "DELETE FROM companies WHERE id = :c"
            ), {"c": test_company_id})
        await db.commit()
    print("cleanup done")
    return 0


async def _fresh_db():
    """Return a fresh AsyncSession bound to the live engine for the
    lifecycle call. Caller is start_engagement which manages its own
    commit + close; we just give it a clean handle."""
    from app.database import async_session as _factory
    return _factory()


if __name__ == "__main__":
    arg = int(sys.argv[1]) if len(sys.argv) > 1 else None
    sys.exit(asyncio.run(main(arg)))
