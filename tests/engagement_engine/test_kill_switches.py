"""Tests for kill-switch gate logic.

These exercise the _check_all_gates pure function (no DB) using synthetic
_ActionContext objects. The end-to-end SQL-backed
`check_dispatch_eligibility()` is exercised by the staging Postgres
verification path; here we test the gate priority + per-gate behavior.
"""
from datetime import datetime, timedelta, timezone
import pytest

from app.engagement_engine.kill_switches import (
    check_gates_for_context,
    _ActionContext,
)


def _ctx(**overrides) -> _ActionContext:
    """Build a default 'everything is OK' context, with overrides."""
    now = datetime.now(timezone.utc)
    defaults = dict(
        action_id=1,
        engagement_id=10,
        engagement_status="active",
        last_reply_at=None,
        contact_id=100,
        contact_email="tim@example.com",
        contact_phone="+14155551234",
        contact_linkedin_url=None,
        contact_do_not_contact=False,
        contact_outreach_owner="engagement_engine",
        contact_email_status="valid",
        company_id=200,
        company_do_not_contact=False,
        company_sequence_resume_at=None,
        channel_id=1,
        channel_code="email",
        channel_is_paused=False,
        action_created_at=now - timedelta(minutes=10),
        action_stale_after=now + timedelta(hours=24),
        action_superseded_by=None,
        action_recipient_email="tim@example.com",
        action_recipient_phone=None,
        action_recipient_linkedin_url=None,
    )
    defaults.update(overrides)
    return _ActionContext(**defaults)


# ── Kill switches ───────────────────────────────────────────────────────────

def test_default_context_passes():
    result = check_gates_for_context(_ctx())
    assert result.eligible is True


def test_engagement_terminal_blocks():
    result = check_gates_for_context(_ctx(engagement_status="terminal"))
    assert result.eligible is False
    assert result.block_reason == "engagement_terminal"


def test_engagement_paused_blocks():
    result = check_gates_for_context(_ctx(engagement_status="paused"))
    assert result.eligible is False
    assert result.block_reason == "engagement_paused"


def test_contact_do_not_contact_blocks():
    result = check_gates_for_context(_ctx(contact_do_not_contact=True))
    assert result.eligible is False
    assert result.block_reason == "contact_do_not_contact"


def test_company_do_not_contact_blocks():
    result = check_gates_for_context(_ctx(company_do_not_contact=True))
    assert result.eligible is False
    assert result.block_reason == "company_do_not_contact"


def test_company_snoozed_blocks():
    result = check_gates_for_context(_ctx(
        company_sequence_resume_at=datetime.now(timezone.utc) + timedelta(days=14)))
    assert result.eligible is False
    assert result.block_reason == "company_snoozed"


def test_company_snooze_expired_passes():
    result = check_gates_for_context(_ctx(
        company_sequence_resume_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    assert result.eligible is True


def test_bounced_email_blocks_email_channel():
    result = check_gates_for_context(_ctx(contact_email_status="bounced"))
    assert result.eligible is False
    assert result.block_reason == "email_bounced"


def test_bounced_email_does_not_block_other_channels():
    result = check_gates_for_context(_ctx(
        contact_email_status="bounced", channel_code="sms",
        action_recipient_email=None))
    assert result.eligible is True


def test_outreach_owner_legacy_blocks():
    """During cutover, contacts owned by 'legacy' should not be processed
    by the new engine."""
    result = check_gates_for_context(_ctx(contact_outreach_owner="legacy"))
    assert result.eligible is False
    assert "outreach_owner=legacy" in result.block_reason


def test_outreach_owner_paused_blocks():
    result = check_gates_for_context(_ctx(contact_outreach_owner="paused"))
    assert result.eligible is False


def test_outreach_owner_white_glove_blocks():
    result = check_gates_for_context(_ctx(contact_outreach_owner="white_glove"))
    assert result.eligible is False


def test_channel_paused_blocks():
    result = check_gates_for_context(_ctx(channel_is_paused=True))
    assert result.eligible is False
    assert "channel_paused" in result.block_reason


# ── Stale-action checks ─────────────────────────────────────────────────────

def test_stale_post_reply_blocks():
    """Action created at T0; reply arrived at T0+5min. Action should be
    stale-skipped because the prospect already replied."""
    now = datetime.now(timezone.utc)
    result = check_gates_for_context(_ctx(
        action_created_at=now - timedelta(hours=1),
        last_reply_at=now - timedelta(minutes=30),
    ))
    assert result.eligible is False
    assert result.block_reason == "stale_post_reply"


def test_old_reply_does_not_make_action_stale():
    """Reply was before the action was created — not a stale-post-reply
    case (this is normal — we may legitimately reply to an old reply)."""
    now = datetime.now(timezone.utc)
    result = check_gates_for_context(_ctx(
        action_created_at=now - timedelta(minutes=10),
        last_reply_at=now - timedelta(hours=2),
    ))
    assert result.eligible is True


def test_superseded_action_blocks():
    result = check_gates_for_context(_ctx(action_superseded_by=999))
    assert result.eligible is False
    assert result.block_reason == "superseded"


def test_stale_too_old_blocks():
    """An action whose stale_after has passed should be skipped."""
    now = datetime.now(timezone.utc)
    result = check_gates_for_context(_ctx(
        action_stale_after=now - timedelta(hours=1),
    ))
    assert result.eligible is False
    assert result.block_reason == "stale_too_old"


# ── Recipient drift (the B3 fix) ────────────────────────────────────────────

def test_recipient_email_drift_blocks():
    """Action scheduled with one email; contact's email has since changed.
    Dispatcher MUST block — sending to the stale address is wrong."""
    result = check_gates_for_context(_ctx(
        action_recipient_email="old@example.com",
        contact_email="new@example.com",
    ))
    assert result.eligible is False
    assert result.block_reason == "recipient_drift_email"


def test_recipient_phone_drift_blocks():
    result = check_gates_for_context(_ctx(
        action_recipient_phone="+14155550000",
        contact_phone="+14155551111",
    ))
    assert result.eligible is False
    assert result.block_reason == "recipient_drift_phone"


def test_recipient_linkedin_drift_blocks():
    result = check_gates_for_context(_ctx(
        action_recipient_linkedin_url="https://linkedin.com/in/old",
        contact_linkedin_url="https://linkedin.com/in/new",
    ))
    assert result.eligible is False
    assert result.block_reason == "recipient_drift_linkedin"


def test_null_recipient_fields_skip_drift_check():
    """If an action has no recipient_phone set (e.g., email-only action),
    a phone-drift check shouldn't fire."""
    result = check_gates_for_context(_ctx(
        action_recipient_phone=None,
        contact_phone="+14155550000",
    ))
    assert result.eligible is True


# ── Priority ordering ───────────────────────────────────────────────────────

def test_terminal_wins_over_drift():
    """If both engagement is terminal AND recipient drifted, the terminal
    reason fires first (cheaper / more semantic)."""
    result = check_gates_for_context(_ctx(
        engagement_status="terminal",
        action_recipient_email="old@x.com",
        contact_email="new@x.com",
    ))
    assert result.eligible is False
    assert result.block_reason == "engagement_terminal"


def test_dnc_wins_over_stale_reply():
    result = check_gates_for_context(_ctx(
        contact_do_not_contact=True,
        action_created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        last_reply_at=datetime.now(timezone.utc) - timedelta(minutes=30),
    ))
    assert result.block_reason == "contact_do_not_contact"
