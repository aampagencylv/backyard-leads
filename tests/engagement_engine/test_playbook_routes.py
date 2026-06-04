"""Tests for playbook editor schemas + helpers.

Endpoint-integration tests run on staging. Here we cover the Pydantic
schemas, the placeholder warning detector, and the template renderer —
the parts with deterministic behavior independent of the DB.
"""
import pytest
from pydantic import ValidationError

from app.routes.engagement_playbook_routes import (
    PlaybookCreate, PlaybookUpdate, PlaybookActionCreate,
    PlaybookActionUpdate, ReorderRequest, TestSendRequest,
    TestSendResponse, PlaybookOut, PlaybookActionOut,
    _placeholder_warnings, _render_template, _to_dict,
)


# ── Schema validation ──────────────────────────────────────────────────────

def test_playbook_create_valid():
    pb = PlaybookCreate(
        name="Cold Pool Builder Outreach",
        description="Used for new pool-builder prospects",
        phase="cold_outreach",
        mode="linear_sequence",
        duration_max_days=30,
    )
    assert pb.mode == "linear_sequence"


def test_playbook_create_rejects_invalid_phase():
    with pytest.raises(ValidationError):
        PlaybookCreate(name="x", phase="not_a_phase", mode="linear_sequence")


def test_playbook_create_rejects_invalid_mode():
    with pytest.raises(ValidationError):
        PlaybookCreate(name="x", phase="cold_outreach", mode="weird_mode")


def test_playbook_create_rejects_empty_name():
    with pytest.raises(ValidationError):
        PlaybookCreate(name="", phase="cold_outreach", mode="linear_sequence")


def test_playbook_create_rejects_extra():
    with pytest.raises(ValidationError):
        PlaybookCreate(
            name="x", phase="cold_outreach", mode="linear_sequence",
            sneaky_field="bad",
        )


def test_playbook_update_all_optional():
    u = PlaybookUpdate()
    assert u.name is None


def test_playbook_update_rejects_phase_change():
    """Phase is intentionally NOT in PlaybookUpdate — changing it requires
    creating a new playbook conceptually."""
    with pytest.raises(ValidationError):
        PlaybookUpdate(phase="meeting_set")  # type: ignore


def test_playbook_action_create_valid_email():
    a = PlaybookActionCreate(
        channel_code="email",
        trigger="scheduled",
        ai_personalization_mode="augmented",
        subject_template="Hi {{first_name}}",
        body_template="Body text",
        day_offset=3,
    )
    assert a.channel_code == "email"
    assert a.day_offset == 3


def test_playbook_action_create_signal_driven_no_day_offset():
    """signal_driven mode is allowed to omit day_offset; the DB trigger
    enforces the coupling between mode + day_offset."""
    a = PlaybookActionCreate(
        channel_code="email", trigger="on_signal",
    )
    assert a.day_offset is None


def test_playbook_action_create_rejects_invalid_trigger():
    with pytest.raises(ValidationError):
        PlaybookActionCreate(channel_code="email", trigger="bad_trigger")


def test_playbook_action_create_rejects_negative_day_offset():
    with pytest.raises(ValidationError):
        PlaybookActionCreate(channel_code="email", day_offset=-1)


def test_playbook_action_create_caps_body_length():
    with pytest.raises(ValidationError):
        PlaybookActionCreate(
            channel_code="email",
            body_template="x" * 50_000,
        )


def test_reorder_request_bounds():
    with pytest.raises(ValidationError):
        ReorderRequest(new_order_index=0)
    with pytest.raises(ValidationError):
        ReorderRequest(new_order_index=300)
    # In range
    r = ReorderRequest(new_order_index=5)
    assert r.new_order_index == 5


def test_test_send_request_accepts_both_modes():
    a = TestSendRequest(contact_id=42)
    b = TestSendRequest(sample_contact={"first_name": "Tim"})
    c = TestSendRequest()  # neither — fine; renders with empty context
    assert a.contact_id == 42
    assert b.sample_contact == {"first_name": "Tim"}
    assert c.contact_id is None


def test_test_send_request_rejects_extra():
    with pytest.raises(ValidationError):
        TestSendRequest(contact_id=1, weird="bad")


# ── Placeholder warning detection ──────────────────────────────────────────

def test_placeholder_warnings_empty():
    assert _placeholder_warnings("") == []
    assert _placeholder_warnings(None) == []
    assert _placeholder_warnings("Hello Tim, no placeholders here") == []


def test_placeholder_warnings_finds_unrendered():
    warnings = _placeholder_warnings(
        "Hi {{first_name}}, your audit at {{audit_url}} is ready."
    )
    assert "{{first_name}}" in warnings
    assert "{{audit_url}}" in warnings


def test_placeholder_warnings_dedupes():
    warnings = _placeholder_warnings(
        "Hi {{first_name}}, hello again {{first_name}}!"
    )
    assert warnings == ["{{first_name}}"]


def test_placeholder_warnings_handles_dotted_names():
    warnings = _placeholder_warnings("Hi {{contact.first_name}}")
    assert "{{contact.first_name}}" in warnings


# ── Template rendering ─────────────────────────────────────────────────────

def test_render_template_basic():
    out = _render_template("Hi {{first_name}}!", {"first_name": "Tim"})
    assert out == "Hi Tim!"


def test_render_template_handles_none():
    assert _render_template(None, {}) is None


def test_render_template_missing_key_renders_empty():
    out = _render_template("Hi {{missing_key}}", {"first_name": "Tim"})
    assert out == "Hi "


def test_render_template_multiple_placeholders():
    out = _render_template(
        "Hi {{first_name}} {{last_name}}, your audit is at {{audit_url}}",
        {"first_name": "Tim", "last_name": "Fox", "audit_url": "https://x.com/1"},
    )
    assert out == "Hi Tim Fox, your audit is at https://x.com/1"


def test_render_template_whitespace_tolerant():
    out = _render_template("Hi {{ first_name }}", {"first_name": "Tim"})
    assert out == "Hi Tim"


def test_render_template_with_none_value():
    out = _render_template("Hi {{first_name}}", {"first_name": None})
    assert out == "Hi "


# ── _to_dict helper ────────────────────────────────────────────────────────

def test_to_dict_none():
    assert _to_dict(None) == {}


def test_to_dict_already_dict():
    assert _to_dict({"a": 1}) == {"a": 1}


def test_to_dict_json_string():
    assert _to_dict('{"a": 1}') == {"a": 1}


def test_to_dict_malformed_json_string():
    assert _to_dict('{"a":') == {}


def test_to_dict_other_types():
    assert _to_dict(42) == {}
    assert _to_dict([1, 2]) == {}
