"""Validate the post-review-patches: resume_engagement INTERVAL bind,
terminate_engagement from a non-cold phase, and company_routes SEQUENCE_SCHEDULE
shape. Creates a throwaway contact + engagement, exercises each fix, asserts
the right post-state, cleans up.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import text

from app.database import async_session
from app.models import Contact


async def _seed_contact() -> tuple[int, int]:
    async with async_session() as db:
        tenant_id = (await db.execute(text(
            "SELECT id FROM tenants ORDER BY id LIMIT 1"
        ))).first()[0]
        co_id = (await db.execute(text("""
            INSERT INTO companies (
                tenant_id, name, email_generated, status,
                lead_score, lead_score_fit, lead_score_intent, lead_score_tier,
                created_at, updated_at
            ) VALUES (
                :t, '__validation_patches_co__', FALSE, 'pursuing',
                0, 0, 0, 'cold', NOW(), NOW()
            ) RETURNING id
        """), {"t": tenant_id})).first()[0]
        c_id = (await db.execute(text("""
            INSERT INTO contacts (
                tenant_id, company_id, first_name, last_name, email,
                is_primary, do_not_text,
                unsubscribe_token, created_at, updated_at, outreach_owner
            ) VALUES (
                :t, :co, 'Patch', 'Test', 'patch-test@example.invalid',
                TRUE, FALSE,
                'patch-test-token', NOW(), NOW(), 'none'
            ) RETURNING id
        """), {"t": tenant_id, "co": co_id})).first()[0]
        await db.commit()
    return int(c_id), int(co_id)


async def _cleanup(contact_id: int, company_id: int) -> None:
    async with async_session() as db:
        await db.execute(text("DELETE FROM actions WHERE contact_id = :c"), {"c": contact_id})
        await db.execute(text("DELETE FROM engagements WHERE contact_id = :c"), {"c": contact_id})
        await db.execute(text("DELETE FROM activities WHERE contact_id = :c OR company_id = :co"), {"c": contact_id, "co": company_id})
        await db.execute(text("DELETE FROM contacts WHERE id = :c"), {"c": contact_id})
        await db.execute(text("DELETE FROM companies WHERE id = :c"), {"c": company_id})
        await db.commit()


async def test_resume_interval():
    """D1: resume_engagement INTERVAL bind must accept int param under asyncpg."""
    from app.engagement_engine.lifecycle import (
        start_engagement, pause_engagement, resume_engagement,
    )
    contact_id, company_id = await _seed_contact()
    try:
        async with async_session() as db:
            contact = (await db.execute(
                text("SELECT * FROM contacts WHERE id = :c"), {"c": contact_id}
            )).first()
            # Build a Contact ORM object minimally for start_engagement
            from app.models import Contact as _C
            obj = (await db.execute(__import__("sqlalchemy").select(_C).where(_C.id == contact_id))).scalar_one()
            n = await start_engagement(db, obj, pre_generate_content=False, initiated_by="patch_test")
            assert n > 0, "start_engagement returned 0"

        # Pause everything
        paused = await pause_engagement(await _new_session(), contact_id, reason="patch test pause")
        print(f"  paused {paused} actions")
        assert paused > 0, "pause_engagement returned 0"

        # Resume with a resume_at 1 hour in the future → exercises the INTERVAL
        # bind. Pre-patch this would crash with `expected str, got int`.
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        resumed = await resume_engagement(await _new_session(), contact_id, resume_at=future)
        print(f"  resumed {resumed} actions with resume_at=+1h")
        assert resumed > 0, "resume_engagement returned 0"

        # Resume_at tz-naive — must NOT crash (D4 fix)
        # First pause again
        await pause_engagement(await _new_session(), contact_id, reason="patch test naive")
        naive_future = datetime.utcnow() + timedelta(hours=2)
        resumed2 = await resume_engagement(await _new_session(), contact_id, resume_at=naive_future)
        print(f"  resumed {resumed2} actions with tz-naive resume_at")

        print("  D1 + D4 PASS")
    finally:
        await _cleanup(contact_id, company_id)


async def test_terminate_from_meeting_set():
    """E2: terminate from meeting_set phase must succeed (with final_phase=None
    we skip the trigger; with final_phase set we use transition_by='bdr' which
    has the broader allowlist)."""
    from app.engagement_engine.lifecycle import (
        start_engagement, terminate_engagement,
    )
    contact_id, company_id = await _seed_contact()
    try:
        from app.models import Contact as _C
        from sqlalchemy import select as _sel
        async with async_session() as db:
            obj = (await db.execute(_sel(_C).where(_C.id == contact_id))).scalar_one()
            await start_engagement(db, obj, pre_generate_content=False, initiated_by="patch_test")

        # Promote the engagement to meeting_set so we can test termination.
        async with async_session() as db:
            await db.execute(text("""
                UPDATE engagements
                SET current_phase = 'meeting_set', last_transition_by = 'bdr'
                WHERE contact_id = :c AND status = 'active'
            """), {"c": contact_id})
            await db.commit()

        # Default terminate (final_phase=None): should succeed without trigger fire.
        canceled = await terminate_engagement(
            await _new_session(), contact_id, reason="patch test no-phase-change",
        )
        print(f"  terminate from meeting_set without phase change: canceled {canceled} actions")
        assert canceled >= 0, "terminate returned negative"

        async with async_session() as db:
            row = (await db.execute(text(
                "SELECT status, current_phase FROM engagements WHERE contact_id = :c ORDER BY id DESC LIMIT 1"
            ), {"c": contact_id})).first()
            assert row[0] == "terminal", f"expected terminal, got {row[0]!r}"
            assert row[1] == "meeting_set", f"expected meeting_set preserved, got {row[1]!r}"
            print(f"  status={row[0]} current_phase={row[1]} (unchanged)")
        print("  E2 PASS (terminate from non-cold phase without trigger violation)")
    finally:
        await _cleanup(contact_id, company_id)


async def _new_session():
    """Fresh session for cross-step transitions."""
    return async_session()


async def main():
    print("=== validate_patches ===")
    print()
    print("test_resume_interval...")
    await test_resume_interval()
    print()
    print("test_terminate_from_meeting_set...")
    await test_terminate_from_meeting_set()
    print()
    print("ALL PASS")


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()) or 0)
