"""Regression tests for the send_email guards.

Reproduces the Texas Remodel Team incident (2026-06-03) at the function
level: a non-email step (call/linkedin/imessage placeholder content)
passed to send_email() MUST be refused before reaching Resend.

These tests don't require a DB or live Resend connection — they exercise
the guard logic in isolation. Run with `pytest tests/test_send_guards.py -v`
from the repo root.
"""
import asyncio
import os
import sys
import pytest

# Make `app` importable when running pytest from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.asyncio
async def test_step_type_call_is_refused():
    """A row with step_type='call' must NEVER reach Resend."""
    from app.services.email_sender import send_email
    r = await send_email(
        to_email="prospect@example.com",
        subject="Call 3",
        body="📞 (555) 555-1234\n\nCall talk track: Hi…",
        from_name="Test", from_firstname="test",
        reply_to_email="test@example.com",
        company_id=0, contact_id=0, email_id=0,
        step_type="call",
    )
    assert r["blocked_by_guard"] is True
    assert r["success"] is False
    assert "step_type=call" in r["error"]


@pytest.mark.asyncio
async def test_step_type_linkedin_is_refused():
    """A row with step_type='linkedin' must NEVER reach Resend."""
    from app.services.email_sender import send_email
    r = await send_email(
        to_email="prospect@example.com",
        subject="LinkedIn step 2",
        body="Connect note (under 280 chars): Hey there…",
        from_name="Test", from_firstname="test",
        reply_to_email="test@example.com",
        company_id=0, contact_id=0, email_id=0,
        step_type="linkedin",
    )
    assert r["blocked_by_guard"] is True


@pytest.mark.asyncio
async def test_step_type_imessage_is_refused():
    """A row with step_type='imessage' must NEVER reach Resend."""
    from app.services.email_sender import send_email
    r = await send_email(
        to_email="prospect@example.com",
        subject="iMessage step 5",
        body="Hey - quick follow up",
        from_name="Test", from_firstname="test",
        reply_to_email="test@example.com",
        company_id=0, contact_id=0, email_id=0,
        step_type="imessage",
    )
    assert r["blocked_by_guard"] is True


@pytest.mark.asyncio
async def test_placeholder_subject_call_n_is_refused():
    """Even without step_type, subject 'Call 3' is a known placeholder."""
    from app.services.email_sender import send_email
    r = await send_email(
        to_email="prospect@example.com", subject="Call 3", body="Some body",
        from_name="Test", from_firstname="test", reply_to_email="test@example.com",
        company_id=0, contact_id=0, email_id=0,
    )
    assert r["blocked_by_guard"] is True


@pytest.mark.asyncio
async def test_placeholder_subject_skipped_is_refused():
    """Subject starting with '[Skipped]' is a known placeholder."""
    from app.services.email_sender import send_email
    r = await send_email(
        to_email="prospect@example.com", subject="[Skipped] Linkedin step 2",
        body="Some body",
        from_name="Test", from_firstname="test", reply_to_email="test@example.com",
        company_id=0, contact_id=0, email_id=0,
    )
    assert r["blocked_by_guard"] is True


@pytest.mark.asyncio
async def test_body_phone_marker_is_refused():
    """Even with a legitimate-looking subject, body starting with 📞 is refused."""
    from app.services.email_sender import send_email
    r = await send_email(
        to_email="prospect@example.com",
        subject="Following up on our conversation",
        body="📞 (555) 555-1234\n\nCall talk track:",
        from_name="Test", from_firstname="test", reply_to_email="test@example.com",
        company_id=0, contact_id=0, email_id=0,
    )
    assert r["blocked_by_guard"] is True


@pytest.mark.asyncio
async def test_body_linkedin_marker_is_refused():
    """Body starting with 'Connect note (under 280 chars):' is refused."""
    from app.services.email_sender import send_email
    r = await send_email(
        to_email="prospect@example.com",
        subject="Following up",
        body="Connect note (under 280 chars):\n\nHey there…",
        from_name="Test", from_firstname="test", reply_to_email="test@example.com",
        company_id=0, contact_id=0, email_id=0,
    )
    assert r["blocked_by_guard"] is True


@pytest.mark.asyncio
async def test_anomaly_score_high_is_refused():
    """A combination of mild signals that together exceed the threshold."""
    from app.services.email_sender import send_email
    # Empty subject (40) + invalid recipient (50) = 90 → blocked
    r = await send_email(
        to_email="garbage",  # invalid_recipient flag fires
        subject="",            # empty_subject flag fires
        body="Hi there",
        from_name="Test", from_firstname="test", reply_to_email="test@example.com",
        company_id=0, contact_id=0, email_id=0,
    )
    assert r["blocked_by_guard"] is True
    assert r["anomaly_score"] >= 60


@pytest.mark.asyncio
async def test_realistic_cold_email_scores_clean():
    """A real cold-outreach email of normal length + clean subject scores 0."""
    from app.services.email_sender import _score_email_anomaly
    body = (
        "Hi Timothy\n\n"
        "I ran a quick AI findability scan on your site this morning and "
        "noticed a couple of things you'd probably want to know.\n\n"
        "When someone asks ChatGPT or Perplexity 'best patio contractors in "
        "Spring, Texas,' Texas Remodel Team isn't getting recommended — even "
        "though your reviews are stronger than the firms that are.\n\n"
        "I posted the full audit here: https://audit.example.com/report/abc123\n\n"
        "Worth 15 minutes to walk through what's fixable?\n\n"
        "— Sebastian"
    )
    score, flags = _score_email_anomaly(
        subject="Quick AI audit for Texas Remodel Team",
        body=body,
        recipient_email="tim@texasremodelteam.com",
    )
    assert score == 0, f"Real cold email scored {score} with flags {flags}"
    assert flags == []


@pytest.mark.asyncio
async def test_anomaly_score_catches_unsubstituted_template_var():
    """Defense: catches the 'I forgot to render the template' failure mode."""
    from app.services.email_sender import _score_email_anomaly
    score, flags = _score_email_anomaly(
        subject="Hi {{first_name}}",
        body="Hi {{first_name}}, I noticed your site...",
        recipient_email="tim@example.com",
    )
    assert "unsubstituted_template_var" in flags
    assert score >= 30
