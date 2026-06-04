"""Tests for Pydantic decision-output schemas.

These tests do NOT require a DB. They verify that:
  - Each schema accepts a valid LLM output
  - Each schema rejects out-of-range values (relevance_score, etc.)
  - extra='forbid' prevents the LLM from sneaking extra fields
  - The schema registry returns the right class per decision_type
  - Every decision_type referenced in DB enum has a schema (no orphans)
"""
import pytest
from pydantic import ValidationError

from app.engagement_engine.schemas import (
    DECISION_TYPE_SCHEMAS,
    schema_for,
    ScoreSignalRelevanceOutput,
    ClassifyReplyOutput,
    WhatToSendOutput,
    GenerateContentOutput,
    GenerateEngagementSummaryOutput,
    SelectNextStepOutput,
    RecommendPhaseTransitionOutput,
    RecommendTierChangeOutput,
    RecommendPauseOutput,
    RecommendPlaybookSwitchOutput,
    DraftReplyOutput,
    DetectFatigueOutput,
)


# ── ScoreSignalRelevanceOutput ──────────────────────────────────────────────

def test_score_signal_relevance_valid():
    out = ScoreSignalRelevanceOutput(
        relevance_score=85,
        summary="GMB review mentions a 3rd location — growth signal",
    )
    assert out.relevance_score == 85


def test_score_signal_relevance_rejects_over_100():
    with pytest.raises(ValidationError):
        ScoreSignalRelevanceOutput(relevance_score=150, summary="x")


def test_score_signal_relevance_rejects_negative():
    with pytest.raises(ValidationError):
        ScoreSignalRelevanceOutput(relevance_score=-5, summary="x")


def test_score_signal_relevance_summary_capped():
    with pytest.raises(ValidationError):
        ScoreSignalRelevanceOutput(relevance_score=50, summary="x" * 500)


def test_score_signal_relevance_rejects_extra_fields():
    with pytest.raises(ValidationError):
        ScoreSignalRelevanceOutput(
            relevance_score=50,
            summary="ok",
            extra_field="this should be rejected",
        )


# ── ClassifyReplyOutput ─────────────────────────────────────────────────────

def test_classify_reply_valid():
    out = ClassifyReplyOutput(
        intent="interested",
        confidence=0.92,
        requires_human_review=False,
        reasoning="Reply explicitly asks for a meeting",
    )
    assert out.intent == "interested"


def test_classify_reply_with_phase_transition():
    out = ClassifyReplyOutput(
        intent="interested",
        confidence=0.85,
        requires_human_review=False,
        suggested_phase_transition="meeting_set",
        reasoning="x",
    )
    assert out.suggested_phase_transition == "meeting_set"


def test_classify_reply_rejects_invalid_intent():
    with pytest.raises(ValidationError):
        ClassifyReplyOutput(
            intent="happy",  # not a legal enum value
            confidence=0.8,
            requires_human_review=False,
            reasoning="x",
        )


def test_classify_reply_rejects_confidence_over_1():
    with pytest.raises(ValidationError):
        ClassifyReplyOutput(
            intent="neutral",
            confidence=1.5,
            requires_human_review=False,
            reasoning="x",
        )


# ── WhatToSendOutput ────────────────────────────────────────────────────────

def test_what_to_send_no_action():
    out = WhatToSendOutput(
        should_act=False,
        reasoning="Signal too noisy to warrant action",
    )
    assert out.should_act is False
    assert out.channel is None


def test_what_to_send_email_action():
    out = WhatToSendOutput(
        should_act=True,
        channel="email",
        subject="Quick follow-up",
        body="Hi Tim — saw you just opened a 3rd location...",
        delay_hours=2,
        requires_human_review=False,
        reasoning="GMB expansion signal",
    )
    assert out.channel == "email"
    assert out.delay_hours == 2


def test_what_to_send_rejects_invalid_channel():
    with pytest.raises(ValidationError):
        WhatToSendOutput(
            should_act=True,
            channel="carrier_pigeon",  # not a legal enum
            reasoning="x",
        )


def test_what_to_send_rejects_excessive_delay():
    with pytest.raises(ValidationError):
        WhatToSendOutput(
            should_act=True,
            channel="email",
            delay_hours=10000,  # over 30 day cap
            reasoning="x",
        )


def test_what_to_send_rejects_body_too_long():
    with pytest.raises(ValidationError):
        WhatToSendOutput(
            should_act=True,
            channel="email",
            subject="x",
            body="x" * 5000,
            reasoning="r",
        )


# ── RecommendPhaseTransitionOutput ──────────────────────────────────────────

def test_recommend_phase_transition_valid():
    out = RecommendPhaseTransitionOutput(
        should_transition=True,
        target_phase="meeting_set",
        reasoning="Prospect explicitly confirmed meeting",
    )
    assert out.target_phase == "meeting_set"


def test_recommend_phase_transition_rejects_illegal_phase():
    with pytest.raises(ValidationError):
        RecommendPhaseTransitionOutput(
            should_transition=True,
            target_phase="hot_lead",  # not in legal enum
            reasoning="x",
        )


# ── DraftReplyOutput ────────────────────────────────────────────────────────

def test_draft_reply_valid():
    out = DraftReplyOutput(
        draft_subject="Re: Your inquiry",
        draft_body="Thanks for reaching out. Let me check on the timeline...",
        notes_for_bdr="Prospect wants Q3 — verify availability before sending",
    )
    assert out.draft_subject.startswith("Re:")


# ── DetectFatigueOutput ─────────────────────────────────────────────────────

def test_detect_fatigue_valid():
    out = DetectFatigueOutput(
        fatigue_score=75,
        recommended_action="pause",
        reasoning="3 emails opened but never replied; declining open rate",
    )
    assert out.recommended_action == "pause"


def test_detect_fatigue_default_action_continue():
    out = DetectFatigueOutput(
        fatigue_score=20,
        reasoning="Engagement looks normal",
    )
    assert out.recommended_action == "continue"


# ── Schema registry ─────────────────────────────────────────────────────────

def test_schema_registry_returns_correct_class():
    assert schema_for("score_signal_relevance") is ScoreSignalRelevanceOutput
    assert schema_for("what_to_send") is WhatToSendOutput
    assert schema_for("classify_reply") is ClassifyReplyOutput
    assert schema_for("recommend_phase_transition") is RecommendPhaseTransitionOutput


def test_schema_registry_raises_keyerror_for_unknown_type():
    with pytest.raises(KeyError):
        schema_for("not_a_real_decision_type")


def test_schema_registry_covers_all_db_decision_types():
    """Every decision_type in the migrate_engagement_engine_v1.py CHECK
    constraint must have a schema in the registry. If this fails, someone
    added a decision_type to the DB enum but not the schema registry."""
    # Source of truth: the CHECK constraint values in the migration script
    db_enum_values = {
        "score_signal_relevance", "what_to_send", "when_to_send",
        "classify_reply", "draft_reply",
        "recommend_playbook_switch", "recommend_phase_transition",
        "recommend_tier_change", "recommend_pause",
        "generate_engagement_summary", "generate_content",
        "select_next_step", "detect_fatigue",
    }
    schema_keys = set(DECISION_TYPE_SCHEMAS.keys())
    missing = db_enum_values - schema_keys
    assert not missing, f"DB enum values missing from schema registry: {missing}"
    # Reverse check: no orphan schemas
    extra = schema_keys - db_enum_values
    assert not extra, f"Schema registry has unknown decision_types: {extra}"
