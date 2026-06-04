"""Per-decision-type prompt builders.

Each function returns a (system_prompt, user_prompt) tuple ready to feed
into LLMProvider.complete(). All builders:

  - Prepend UNTRUSTED_CONTENT_SYSTEM_PROMPT_PREFIX to the system prompt
    so the LLM treats <untrusted_content> blocks as data, not instructions
  - Wrap external/scraped/reply content in `wrap_untrusted()` blocks
  - Embed a stringified Pydantic schema so the LLM knows the expected
    output shape (works across providers without tool-use)
  - Keep the trusted (instruction) portion small + canonical so per-call
    overhead is minimal

The prompts are deliberately minimal in Phase 4. Tier 3 features
(LLM-augmented personalization with research-woven copy) land in Phase 8.
"""
from __future__ import annotations
import json
from typing import Iterable
from pydantic import BaseModel

from app.engagement_engine.validators import (
    wrap_untrusted,
    UNTRUSTED_CONTENT_SYSTEM_PROMPT_PREFIX,
)


def _schema_block(schema: type[BaseModel]) -> str:
    """Pretty-print the JSON schema so the LLM has a precise output shape."""
    return json.dumps(schema.model_json_schema(), indent=2)


def _output_instruction(schema_name: str) -> str:
    return (
        f"\nReturn ONLY a JSON object matching the {schema_name} schema. "
        f"Do not include any surrounding prose, markdown fences, or "
        f"explanation outside the JSON."
    )


# ── score_signal_relevance ──────────────────────────────────────────────────

def build_score_signal_prompt(
    *,
    signal_type_code: str,
    raw_signal_data: dict,
    engagement_summary: str | None,
    contact_name: str,
    company_name: str,
    schema: type[BaseModel],
) -> tuple[str, str]:
    system = UNTRUSTED_CONTENT_SYSTEM_PROMPT_PREFIX + (
        "\n\nYou score lead-prospect signals on a 0-100 relevance scale "
        "for an outbound sales engagement engine. 0 = ignore; 50 = neutral; "
        "70+ = trigger action; 90+ = urgent. Be conservative: meaningful "
        "business signals (expansion, hiring, leadership changes, "
        "negative reviews) score 70+; routine activity (one open, "
        "minor content update) stays under 60."
    )
    user = (
        f"Score this signal for {contact_name} at {company_name}.\n\n"
        f"Signal type: {signal_type_code}\n\n"
        f"{wrap_untrusted('signal_data', json.dumps(raw_signal_data, default=str))}\n\n"
        f"Engagement context:\n"
        f"{wrap_untrusted('engagement_summary', engagement_summary or '(no prior context)')}\n\n"
        f"Output schema (ScoreSignalRelevanceOutput):\n"
        f"```json\n{_schema_block(schema)}\n```\n"
        f"{_output_instruction(schema.__name__)}"
    )
    return system, user


# ── classify_reply ──────────────────────────────────────────────────────────

def build_classify_reply_prompt(
    *,
    reply_body: str,
    reply_subject: str,
    engagement_summary: str | None,
    last_sent_subject: str | None,
    schema: type[BaseModel],
) -> tuple[str, str]:
    system = UNTRUSTED_CONTENT_SYSTEM_PROMPT_PREFIX + (
        "\n\nYou classify the intent of inbound replies for a sales "
        "engagement engine. Pick the intent that BEST fits the reply text. "
        "Set requires_human_review=true for: price negotiations, "
        "competitor mentions, complex objections, anything legally "
        "sensitive, or anything you're <70% confident about."
    )
    user = (
        f"Classify this reply.\n\n"
        f"Last outbound subject we sent: {last_sent_subject or '(unknown)'}\n\n"
        f"Reply subject:\n{wrap_untrusted('reply_subject', reply_subject)}\n\n"
        f"Reply body:\n{wrap_untrusted('reply_body', reply_body)}\n\n"
        f"Engagement context:\n"
        f"{wrap_untrusted('engagement_summary', engagement_summary or '(no prior context)')}\n\n"
        f"Output schema (ClassifyReplyOutput):\n"
        f"```json\n{_schema_block(schema)}\n```\n"
        f"{_output_instruction(schema.__name__)}"
    )
    return system, user


# ── what_to_send (the expensive decision) ───────────────────────────────────

def build_what_to_send_prompt(
    *,
    signal_summary: str,
    signal_data: dict,
    engagement_summary: str | None,
    contact_name: str,
    company_name: str,
    recent_signals: list[dict],
    recent_actions: list[dict],
    bdr_notes: str | None,
    legal_transitions: list[str],
    available_channels: list[str],
    schema: type[BaseModel],
) -> tuple[str, str]:
    system = UNTRUSTED_CONTENT_SYSTEM_PROMPT_PREFIX + (
        "\n\nYou are deciding the next outreach action for a sales prospect. "
        "Be CONSERVATIVE: it's better to skip a marginal opportunity than "
        "burn a prospect with a poorly-timed message. Set should_act=true "
        "only when you have a clear, prospect-relevant angle (e.g., a new "
        "review, expansion, hiring spike — not just 'they opened an email'). "
        "Set requires_human_review=true for: price discussions, custom "
        "proposals, objection handling, or anything you're <80% confident "
        "about."
    )

    user = (
        f"Decide whether (and how) to reach out to {contact_name} at {company_name}.\n\n"
        f"Triggering signal: {signal_summary}\n"
        f"Signal data: {wrap_untrusted('signal_data', json.dumps(signal_data, default=str))}\n\n"
        f"Engagement context:\n"
        f"{wrap_untrusted('engagement_summary', engagement_summary or '(no prior context)')}\n\n"
        f"Recent signals (last 6):\n"
        f"{wrap_untrusted('recent_signals', json.dumps(recent_signals[:6], default=str))}\n\n"
        f"Recent outbound actions (last 12):\n"
        f"{wrap_untrusted('recent_actions', json.dumps(recent_actions[:12], default=str))}\n\n"
        f"BDR notes:\n"
        f"{wrap_untrusted('bdr_notes', bdr_notes or '(none)')}\n\n"
        f"Available channels: {available_channels}\n"
        f"Legal phase transitions from here: {legal_transitions}\n\n"
        f"Output schema (WhatToSendOutput):\n"
        f"```json\n{_schema_block(schema)}\n```\n"
        f"{_output_instruction(schema.__name__)}"
    )
    return system, user


# ── generate_engagement_summary ─────────────────────────────────────────────

def build_engagement_summary_prompt(
    *,
    contact_name: str,
    company_name: str,
    current_phase: str,
    recent_signals: list[dict],
    recent_actions: list[dict],
    bdr_notes: str | None,
    schema: type[BaseModel],
) -> tuple[str, str]:
    system = UNTRUSTED_CONTENT_SYSTEM_PROMPT_PREFIX + (
        "\n\nYou maintain a one-paragraph summary capturing where a sales "
        "engagement currently stands. The summary is read by future "
        "AI decisions, so include: current phase, what we've tried, what "
        "the prospect has shown, BDR's read on the situation. Be factual; "
        "avoid speculation."
    )
    user = (
        f"Write the current engagement summary for {contact_name} at "
        f"{company_name}. Current phase: {current_phase}.\n\n"
        f"Recent signals:\n"
        f"{wrap_untrusted('recent_signals', json.dumps(recent_signals[:10], default=str))}\n\n"
        f"Recent actions:\n"
        f"{wrap_untrusted('recent_actions', json.dumps(recent_actions[:10], default=str))}\n\n"
        f"BDR notes:\n"
        f"{wrap_untrusted('bdr_notes', bdr_notes or '(none)')}\n\n"
        f"Output schema (GenerateEngagementSummaryOutput):\n"
        f"```json\n{_schema_block(schema)}\n```\n"
        f"{_output_instruction(schema.__name__)}"
    )
    return system, user


# ── recommend_phase_transition ──────────────────────────────────────────────

def build_phase_transition_prompt(
    *,
    contact_name: str,
    company_name: str,
    current_phase: str,
    current_status: str,
    engagement_summary: str | None,
    recent_signals: list[dict],
    legal_target_phases: list[str],
    schema: type[BaseModel],
) -> tuple[str, str]:
    system = UNTRUSTED_CONTENT_SYSTEM_PROMPT_PREFIX + (
        "\n\nYou decide whether an engagement should transition to a new "
        "phase. Be CONSERVATIVE — transitions are sticky and shape weeks "
        "of future outreach. Recommend a transition only when there's "
        "clear evidence the prospect has moved (meeting booked, explicitly "
        "declined, signed with a competitor, etc.). If unclear, "
        "should_transition=false.\n\n"
        "You MUST pick target_phase from the provided legal_target_phases "
        "list OR set should_transition=false. The DB will reject any "
        "transition not in the list."
    )
    legal_list = json.dumps(legal_target_phases)
    user = (
        f"Engagement: {contact_name} at {company_name}\n"
        f"Current phase: {current_phase} (status: {current_status})\n"
        f"Legal target phases: {legal_list}\n\n"
        f"Engagement summary:\n"
        f"{wrap_untrusted('engagement_summary', engagement_summary or '(no prior context)')}\n\n"
        f"Recent signals:\n"
        f"{wrap_untrusted('recent_signals', json.dumps(recent_signals[:6], default=str))}\n\n"
        f"Output schema (RecommendPhaseTransitionOutput):\n"
        f"```json\n{_schema_block(schema)}\n```\n"
        f"{_output_instruction(schema.__name__)}"
    )
    return system, user


# ── detect_fatigue (batch nightly) ──────────────────────────────────────────

def build_detect_fatigue_prompt(
    *,
    contact_name: str,
    company_name: str,
    days_since_last_engagement: int | None,
    sends_last_30d: int,
    opens_last_30d: int,
    replies_last_30d: int,
    schema: type[BaseModel],
) -> tuple[str, str]:
    system = UNTRUSTED_CONTENT_SYSTEM_PROMPT_PREFIX + (
        "\n\nYou detect prospect-fatigue patterns: declining engagement, "
        "many sends with no responses, opens trending down. fatigue_score "
        "0-100 (0=engaged, 100=clearly fatigued). Recommend 'pause' for "
        "fatigue >70, 'switch_channel' for moderate fatigue with single-"
        "channel exhaustion, 'switch_playbook' for fatigue with stale "
        "playbook fit. 'continue' is the default."
    )
    user = (
        f"Assess fatigue for {contact_name} at {company_name}:\n\n"
        f"  - Sends in last 30 days: {sends_last_30d}\n"
        f"  - Opens in last 30 days: {opens_last_30d}\n"
        f"  - Replies in last 30 days: {replies_last_30d}\n"
        f"  - Days since last engagement: {days_since_last_engagement}\n\n"
        f"Output schema (DetectFatigueOutput):\n"
        f"```json\n{_schema_block(schema)}\n```\n"
        f"{_output_instruction(schema.__name__)}"
    )
    return system, user
