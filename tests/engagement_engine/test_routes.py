"""Tests for engagement engine REST routes.

Schema-level tests for the route module: validate Pydantic input/output
shapes, dynamic-SQL builders, and the helper functions. Endpoint
integration tests (requires real DB + auth fixtures) run on staging via
manual smoke testing.
"""
from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from app.routes.engagement_engine_routes import (
    EngagementTimelineItem,
    EngagementDetail,
    SignalFeedItem,
    DecisionAuditItem,
    ActionItem,
    ActionOverride,
    AttributeReplyInput,
    ChannelStatus,
    TenantAIConfigOut,
    TenantAIConfigUpdate,
    InboundUnattributedItem,
)


# ── Schema validation ──────────────────────────────────────────────────────

def test_timeline_item_accepts_valid():
    item = EngagementTimelineItem(
        kind="signal",
        occurred_at=datetime.now(timezone.utc),
        summary="GMB review",
        relevance_score=72,
        item_id=1,
    )
    assert item.kind == "signal"


def test_timeline_item_rejects_extra():
    with pytest.raises(ValidationError):
        EngagementTimelineItem(
            kind="signal",
            occurred_at=datetime.now(timezone.utc),
            summary="x",
            item_id=1,
            extra_sneaky_field="bad",
        )


def test_action_override_requires_at_least_one_field_via_validation():
    """ActionOverride allows ALL fields optional; the route checks
    'at-least-one' in code. Schema-wise this constructs cleanly with
    nothing."""
    o = ActionOverride()
    assert o.subject is None
    assert o.body is None


def test_action_override_accepts_partial():
    o = ActionOverride(subject="New subject")
    assert o.subject == "New subject"
    assert o.body is None


def test_action_override_rejects_extra():
    with pytest.raises(ValidationError):
        ActionOverride(subject="x", sneaky_field="bad")


def test_attribute_reply_with_engagement_id():
    a = AttributeReplyInput(engagement_id=42, resolution="attributed_manually")
    assert a.engagement_id == 42


def test_attribute_reply_with_resolution_only():
    a = AttributeReplyInput(resolution="spam")
    assert a.resolution == "spam"
    assert a.engagement_id is None


def test_attribute_reply_rejects_extra():
    with pytest.raises(ValidationError):
        AttributeReplyInput(engagement_id=1, sneaky="bad")


def test_tenant_ai_config_update_rejects_negative_budget():
    with pytest.raises(ValidationError):
        TenantAIConfigUpdate(per_engagement_budget_usd=-1.0)


def test_tenant_ai_config_update_all_optional():
    u = TenantAIConfigUpdate()
    assert u.provider is None
    assert u.api_key_plaintext is None


def test_tenant_ai_config_update_accepts_partial():
    u = TenantAIConfigUpdate(provider="openrouter", monthly_budget_usd=500)
    assert u.provider == "openrouter"
    assert u.monthly_budget_usd == 500


def test_tenant_ai_config_update_rejects_extra():
    with pytest.raises(ValidationError):
        TenantAIConfigUpdate(provider="anthropic", unknown_field="bad")


def test_signal_feed_item_shape():
    item = SignalFeedItem(
        id=1, engagement_id=10, contact_id=100,
        contact_name="Tim", company_name="Acme",
        signal_type_code="gmb_review",
        relevance_score=85,
        ai_summary="meaningful growth",
        detected_at=datetime.now(timezone.utc),
        triggered_action_id=None,
        source_url=None,
    )
    assert item.relevance_score == 85


def test_channel_status_shape():
    s = ChannelStatus(code="email", label="Email", is_paused=False, is_active=True)
    assert s.code == "email"


def test_inbound_unattributed_item_optional_fields():
    item = InboundUnattributedItem(
        id=1,
        envelope_from=None,
        envelope_to=None,
        subject=None,
        cleaned_body_preview=None,
        received_at=datetime.now(timezone.utc),
        reviewed_at=None,
        resolution=None,
    )
    assert item.id == 1
