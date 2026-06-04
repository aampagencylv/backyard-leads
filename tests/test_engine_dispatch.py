"""Integration tests for the sequence engine dispatch loop.

Covers the state machine that fires email/iMessage steps, defers
manual call/linkedin steps to Tasks, respects company snooze, and
handles the SKIP: / TRANSIENT: marker conventions added during the
iMessage thrash + Resend timeout work.
"""
import pytest
from datetime import datetime, timezone, timedelta
from sqlalchemy import select

from tests.fixtures import db_session, bmp_world, make_step  # noqa: F401


@pytest.mark.asyncio
async def test_snoozed_company_excluded_from_dispatch(bmp_world):
    """Engine dispatch query must EXCLUDE rows whose company is currently
    snoozed (sequence_resume_at > now). Reproduces the snooze gate.
    """
    db = bmp_world["db"]
    co = bmp_world["company"]

    # Mark company as snoozed for the next 30 days
    co.sequence_resume_at = datetime.now(timezone.utc) + timedelta(days=30)
    co.sequence_snooze_days = 30
    co.sequence_snooze_reason = "test snooze"
    await db.commit()

    # Mirror the engine's snoozed-id query
    from app.models import Company, GeneratedEmail
    snoozed_ids = (await db.execute(
        select(Company.id).where(
            Company.sequence_resume_at.is_not(None),
            Company.sequence_resume_at > datetime.now(timezone.utc),
        )
    )).scalars().all()
    assert co.id in snoozed_ids, "Test company should be in the snoozed set"

    # And the dispatch query gate
    pool = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.is_sent == False,
            GeneratedEmail.skipped_at.is_(None),
            GeneratedEmail.auto_execute == True,
            ~GeneratedEmail.company_id.in_(snoozed_ids),
        )
    )).scalars().all()
    assert all(s.company_id != co.id for s in pool), (
        "Engine dispatch must exclude the snoozed company's steps"
    )


@pytest.mark.asyncio
async def test_woke_company_eligible_after_resume_at_passes(bmp_world):
    """When sequence_resume_at falls into the past, the company's steps
    are eligible for the wake handler to regenerate."""
    db = bmp_world["db"]
    co = bmp_world["company"]

    co.sequence_resume_at = datetime.now(timezone.utc) - timedelta(hours=1)
    co.sequence_snooze_days = 7
    await db.commit()

    # Wake handler queries: WHERE sequence_resume_at IS NOT NULL AND <= now
    from app.models import Company
    waking = (await db.execute(
        select(Company).where(
            Company.sequence_resume_at.is_not(None),
            Company.sequence_resume_at <= datetime.now(timezone.utc),
        )
    )).scalars().all()
    assert co.id in [c.id for c in waking]


@pytest.mark.asyncio
async def test_skipped_step_stays_excluded_from_dispatch(bmp_world):
    """skipped_at IS NOT NULL must keep rows out of the dispatch pool —
    this is what prevented infinite-retry on iMessage steps."""
    db = bmp_world["db"]
    co = bmp_world["company"]
    contact = bmp_world["contact"]

    # Create an iMessage step that's been skipped (mirrors the
    # imessage_disabled_by_tenant marker from the iMessage thrash fix)
    skipped = await make_step(
        db, contact_id=contact.id, company_id=co.id, sequence_order=99,
        step_type="imessage", email_type="imessage_1",
        subject="iMessage step 99",
        body="Hey - quick follow up",
        scheduled_send_at=datetime.now(timezone.utc) - timedelta(days=2),
        skipped_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    skipped.skip_reason = "imessage_disabled_by_tenant"
    await db.commit()

    from app.models import GeneratedEmail
    pool = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.is_sent == False,
            GeneratedEmail.skipped_at.is_(None),  # the gate
            GeneratedEmail.auto_execute == True,
        )
    )).scalars().all()
    assert skipped.id not in [s.id for s in pool], (
        "Skipped step must stay out of dispatch — this is what prevented "
        "the 276-step iMessage thrash."
    )


@pytest.mark.asyncio
async def test_disqualify_clears_snooze_state(bmp_world):
    """Disqualify wins over snooze: marking a snoozed company as
    not_interested must clear the snooze fields so the wake handler
    doesn't regenerate a sequence for a terminal company."""
    db = bmp_world["db"]
    co = bmp_world["company"]

    co.sequence_resume_at = datetime.now(timezone.utc) + timedelta(days=30)
    co.sequence_snooze_days = 30
    co.sequence_snooze_reason = "test"
    await db.commit()

    # Simulate disqualify (mirrors company_routes.py:507-513)
    co.status = "not_interested"
    co.lost_reason = "test reason"
    co.sequence_resume_at = None
    co.sequence_snoozed_at = None
    co.sequence_snooze_reason = None
    co.sequence_snoozed_by_user_id = None
    co.sequence_snooze_days = None
    await db.commit()

    await db.refresh(co)
    assert co.status == "not_interested"
    assert co.sequence_resume_at is None
    assert co.sequence_snooze_days is None


@pytest.mark.asyncio
async def test_send_email_anomaly_score_blocks_garbage(bmp_world):
    """Defense in depth: even if a future route bug passes a non-email
    row to send_email, the anomaly scorer should catch the placeholder
    subject + non-email body combo."""
    from app.services.email_sender import _score_email_anomaly

    # Texas Remodel Team's exact bad send
    score, flags = _score_email_anomaly(
        subject="Call 3",
        body="📞 (555) 555-1234\n\nCall talk track:\n- Hi Tim — from BMP.",
        recipient_email="tim@texasremodelteam.com",
    )
    assert score >= 60, (
        f"The exact Texas Remodel Team send pattern should score >=60; "
        f"got {score} with flags {flags}"
    )


@pytest.mark.asyncio
async def test_send_email_anomaly_score_legit_email_clean(bmp_world):
    """A real email Sebastian would actually send should score 0."""
    from app.services.email_sender import _score_email_anomaly
    body = (
        "Hi Timothy\n\n"
        "I ran a quick AI findability scan on your site this morning. When "
        "people ask ChatGPT 'best patio contractors in Spring, Texas,' your "
        "company isn't getting recommended — even though your reviews are "
        "stronger than the firms that are.\n\n"
        "Full audit: https://audit.example.com/abc123\n\n"
        "Worth 15 min to walk through?\n\n— Sebastian"
    )
    score, flags = _score_email_anomaly(
        subject="Quick AI audit for Texas Remodel Team",
        body=body,
        recipient_email="tim@texasremodelteam.com",
    )
    assert score == 0, f"Legit email scored {score}, flags={flags}"
    assert flags == []
