"""Pydantic schemas for AI decision outputs.

Every LLM call goes through `LLMProvider.complete(schema=...)` which validates
the response against one of these schemas. Parse failure → retry with
corrective prompt → fallback_provider → ParseError.

The schemas are deliberately constrained:
  - enums for channel/tier/transition choices (LLM can't hallucinate a
    channel that doesn't exist or a phase that's illegal)
  - max-length caps on free-form fields (prevent runaway token output)
  - explicit required vs optional separation

One schema per AI decision_type in `ai_decisions.decision_type` enum.
"""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict


# ════════════════════════════════════════════════════════════════════════════
# Shared types
# ════════════════════════════════════════════════════════════════════════════

ChannelLiteral = Literal["email", "sms", "linkedin", "call_task", "manual"]
TierLiteral = Literal["hot", "warm", "cold", "dormant"]
PhaseLiteral = Literal[
    "cold_outreach", "meeting_set", "post_meeting_nurture",
    "qualified", "customer", "declined", "lost", "dormant"
]
ReplyIntentLiteral = Literal[
    "interested", "not_interested", "needs_more_info", "wrong_person",
    "unsubscribe", "auto_reply", "out_of_office", "later", "neutral"
]


class _StrictBase(BaseModel):
    """Pydantic base with strict mode enabled — no extra fields permitted.
    Prevents LLM from sneaking extra keys past validation."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ════════════════════════════════════════════════════════════════════════════
# Per-decision_type schemas
# ════════════════════════════════════════════════════════════════════════════

class ScoreSignalRelevanceOutput(_StrictBase):
    """decision_type='score_signal_relevance' — cheap model call per new signal."""
    relevance_score: int = Field(ge=0, le=100,
        description="0=ignore, 50=neutral, 100=urgent action")
    summary: str = Field(max_length=240,
        description="One-line summary of what the signal means")


class ClassifyReplyOutput(_StrictBase):
    """decision_type='classify_reply' — categorize an inbound reply's intent."""
    intent: ReplyIntentLiteral
    confidence: float = Field(ge=0.0, le=1.0)
    requires_human_review: bool = Field(
        description="True if the reply needs BDR review before any response"
    )
    suggested_phase_transition: PhaseLiteral | None = Field(
        default=None,
        description="Recommended phase change based on the reply (or None)"
    )
    reasoning: str = Field(max_length=400)


class WhatToSendOutput(_StrictBase):
    """decision_type='what_to_send' — react to a signal with an outreach."""
    should_act: bool = Field(
        description="False if signal doesn't warrant action right now"
    )
    channel: ChannelLiteral | None = None
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=4000)
    task_description: str | None = Field(default=None, max_length=1000,
        description="Required when channel='call_task' or 'manual'")
    delay_hours: int = Field(default=0, ge=0, le=720,  # 30 days max delay
        description="Hours to wait before dispatch (0 = ASAP within send window)")
    requires_human_review: bool = Field(default=False,
        description="True for price discussions, custom proposals, "
                    "repeated objections — sent through BDR approval gate")
    reasoning: str = Field(max_length=600)


class GenerateContentOutput(_StrictBase):
    """decision_type='generate_content' — flesh out a playbook step's
    AI-augmented template with prospect-specific personalization."""
    subject: str = Field(max_length=200)
    body: str = Field(max_length=4000)
    personalization_notes: str | None = Field(default=None, max_length=400,
        description="What specifically was personalized and why")


class GenerateEngagementSummaryOutput(_StrictBase):
    """decision_type='generate_engagement_summary' — the 1-paragraph 'where
    we are with this prospect' that gets fed into every subsequent expensive
    decision. Cheap to refresh; rewritten when a high-relevance signal
    arrives (summary_stale_at)."""
    summary: str = Field(max_length=1200,
        description="One paragraph capturing current engagement state, "
                    "recent activity, BDR notes, and disposition")
    confidence: float = Field(ge=0.0, le=1.0,
        description="How confidently we know where this engagement stands")


class SelectNextStepOutput(_StrictBase):
    """decision_type='select_next_step' — when a playbook has branches,
    which branch should we take?"""
    next_action_index: int = Field(ge=0)
    reasoning: str = Field(max_length=400)


class RecommendPhaseTransitionOutput(_StrictBase):
    """decision_type='recommend_phase_transition' — should this engagement
    move to a new phase? AI must pick from legal transitions only; the FSM
    trigger will reject illegal choices at DB-write time."""
    should_transition: bool
    target_phase: PhaseLiteral | None = None
    reasoning: str = Field(max_length=600)


class RecommendTierChangeOutput(_StrictBase):
    """decision_type='recommend_tier_change' — engagement temperature
    (hot/warm/cold/dormant) drives polling frequency in the signal watcher."""
    target_tier: TierLiteral
    reasoning: str = Field(max_length=400)


class RecommendPauseOutput(_StrictBase):
    """decision_type='recommend_pause' — fatigue detection. Pauses
    engagement for a number of days to avoid burning the prospect out."""
    should_pause: bool
    pause_days: int = Field(default=0, ge=0, le=90)
    reasoning: str = Field(max_length=400)


class RecommendPlaybookSwitchOutput(_StrictBase):
    """decision_type='recommend_playbook_switch' — swap to a different
    playbook (e.g., from cold_outreach to post_meeting_nurture)."""
    should_switch: bool
    target_playbook_name: str | None = Field(default=None, max_length=200,
        description="Name of the playbook to switch to (engine resolves to ID)")
    reasoning: str = Field(max_length=600)


class DraftReplyOutput(_StrictBase):
    """decision_type='draft_reply' — generate a BDR-reviewed reply to an
    inbound message. Always goes through approval gate
    (requires_human_review=True implicit)."""
    draft_subject: str | None = Field(default=None, max_length=200)
    draft_body: str = Field(max_length=4000)
    notes_for_bdr: str | None = Field(default=None, max_length=600,
        description="What context the BDR should know before sending")


class DetectFatigueOutput(_StrictBase):
    """decision_type='detect_fatigue' — batch nightly check for engagements
    showing signs of prospect-fatigue (no opens, no replies, escalating
    silence)."""
    fatigue_score: int = Field(ge=0, le=100,
        description="0=engaged, 100=clearly fatigued")
    recommended_action: Literal["continue", "pause", "switch_channel", "switch_playbook"] = "continue"
    reasoning: str = Field(max_length=400)


# ════════════════════════════════════════════════════════════════════════════
# Schema registry (decision_type → schema class)
# ════════════════════════════════════════════════════════════════════════════

DECISION_TYPE_SCHEMAS: dict[str, type[_StrictBase]] = {
    "score_signal_relevance":      ScoreSignalRelevanceOutput,
    "classify_reply":              ClassifyReplyOutput,
    "what_to_send":                WhatToSendOutput,
    "generate_content":            GenerateContentOutput,
    "generate_engagement_summary": GenerateEngagementSummaryOutput,
    "select_next_step":            SelectNextStepOutput,
    "recommend_phase_transition":  RecommendPhaseTransitionOutput,
    "recommend_tier_change":       RecommendTierChangeOutput,
    "recommend_pause":             RecommendPauseOutput,
    "recommend_playbook_switch":   RecommendPlaybookSwitchOutput,
    "draft_reply":                 DraftReplyOutput,
    "detect_fatigue":              DetectFatigueOutput,
    # 'when_to_send' is bundled into 'what_to_send.delay_hours' — kept as a
    # separate decision_type in the enum for cases where we re-time without
    # changing content, but uses the same WhatToSendOutput schema.
    "when_to_send":                WhatToSendOutput,
}


def schema_for(decision_type: str) -> type[_StrictBase]:
    """Look up the Pydantic schema class for a decision_type.
    Raises KeyError if not found (which would mean the engine code added a
    decision_type to the DB enum but not the schema registry — caught in
    tests)."""
    return DECISION_TYPE_SCHEMAS[decision_type]
