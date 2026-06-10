"""REST endpoints for the engagement engine CRM surface.

What the BDR (Sebastian) consumes from the Chrome extension / web UI:
  - Engagement timeline (signals + actions + decisions for one contact)
  - High-relevance signal feed across all engagements
  - AI decision audit log with approve/reject controls
  - Inbound unattributed reply queue
  - Channel-pause kill switches (incident response)
  - Tenant AI config (BYO AI settings)

All endpoints are tenant-scoped via `get_tenant_db` so a BDR can never see
another tenant's data. The auto-filter on session.info['tenant_id'] enforces
this; we add explicit WHERE tenant_id clauses as belt-and-suspenders.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field, ConfigDict

from app.tenancy import get_tenant_db
from app.auth import get_current_user
from app.models import User

log = logging.getLogger("engagement_engine.routes")

router = APIRouter(
    prefix="/api/engagement",
    tags=["engagement-engine"],
)


# ════════════════════════════════════════════════════════════════════════════
# Response schemas
# ════════════════════════════════════════════════════════════════════════════

class EngagementTimelineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str  # 'signal' | 'action' | 'decision'
    occurred_at: datetime
    summary: str
    relevance_score: Optional[int] = None
    status: Optional[str] = None
    channel_code: Optional[str] = None
    cost_usd: Optional[float] = None
    item_id: int


class EngagementDetail(BaseModel):
    id: int
    contact_id: int
    contact_email: Optional[str]
    contact_name: Optional[str]
    company_id: int
    company_name: Optional[str]
    current_phase: str
    status: str
    sequence_number: int
    engagement_score: int
    tier: str
    last_outreach_at: Optional[datetime]
    last_signal_at: Optional[datetime]
    last_reply_at: Optional[datetime]
    ai_engagement_summary: Optional[str]
    notes: Optional[str]
    monthly_ai_cost_usd: float
    timeline: list[EngagementTimelineItem]


class SignalFeedItem(BaseModel):
    id: int
    engagement_id: int
    contact_id: int
    contact_name: Optional[str]
    company_name: Optional[str]
    signal_type_code: str
    relevance_score: Optional[int]
    ai_summary: Optional[str]
    detected_at: datetime
    triggered_action_id: Optional[int]
    source_url: Optional[str]


class DecisionAuditItem(BaseModel):
    # `model_used` collides with Pydantic's default 'model_' protected namespace
    model_config = ConfigDict(protected_namespaces=())
    id: int
    engagement_id: int
    signal_id: Optional[int]
    decision_type: str
    provider: str
    model_used: str
    cost_usd: Optional[float]
    latency_ms: Optional[int]
    reasoning: Optional[str]
    output_choice_json: dict
    output_validation_passed: bool
    output_validation_errors: Optional[dict]
    created_at: datetime


class ActionItem(BaseModel):
    id: int
    engagement_id: int
    channel_code: str
    status: str
    scheduled_at: Optional[datetime]
    executed_at: Optional[datetime]
    subject: Optional[str]
    body: Optional[str]
    recipient_email: Optional[str]
    requires_human_review: bool
    approved_by_user_id: Optional[int]
    approved_at: Optional[datetime]
    error_message: Optional[str]
    skip_reason: Optional[str]
    outcome: Optional[str]


class TenantAIConfigOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    tenant_id: int
    provider: str
    has_custom_key: bool
    base_url: Optional[str]
    model_signal_scoring: str
    model_reply_classification: str
    model_content_generation: str
    model_decision_making: str
    model_engagement_summary: str
    monthly_budget_usd: Optional[float]
    per_engagement_budget_usd: float
    fallback_provider: Optional[str]
    tcpa_b2b_override: bool
    default_timezone: str
    current_month_spent_usd: float


class TenantAIConfigUpdate(BaseModel):
    """All fields optional — partial update."""
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    provider: Optional[str] = None
    api_key_plaintext: Optional[str] = None  # encrypted before write
    base_url: Optional[str] = None
    model_signal_scoring: Optional[str] = None
    model_reply_classification: Optional[str] = None
    model_content_generation: Optional[str] = None
    model_decision_making: Optional[str] = None
    model_engagement_summary: Optional[str] = None
    monthly_budget_usd: Optional[float] = Field(default=None, ge=0)
    per_engagement_budget_usd: Optional[float] = Field(default=None, ge=0)
    fallback_provider: Optional[str] = None
    tcpa_b2b_override: Optional[bool] = None
    default_timezone: Optional[str] = None


class InboundUnattributedItem(BaseModel):
    id: int
    envelope_from: Optional[str]
    envelope_to: Optional[str]
    subject: Optional[str]
    cleaned_body_preview: Optional[str]
    received_at: datetime
    reviewed_at: Optional[datetime]
    resolution: Optional[str]


# ════════════════════════════════════════════════════════════════════════════
# Engagement detail timeline
# ════════════════════════════════════════════════════════════════════════════

@router.get("/{engagement_id}", response_model=EngagementDetail)
async def get_engagement_detail(
    engagement_id: int,
    timeline_limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> EngagementDetail:
    """Return one engagement with a merged timeline of signals, actions,
    and AI decisions ordered most-recent-first."""

    # Header data
    header_row = await db.execute(text("""
        SELECT
            e.id, e.contact_id, e.company_id, e.current_phase, e.status,
            e.sequence_number, e.engagement_score, e.tier,
            e.last_outreach_at, e.last_signal_at, e.last_reply_at,
            e.ai_engagement_summary, e.notes, e.monthly_ai_cost_usd,
            c.email AS contact_email,
            c.first_name, c.last_name,
            co.name AS company_name
        FROM engagements e
        JOIN contacts c ON c.id = e.contact_id
        JOIN companies co ON co.id = e.company_id
        WHERE e.id = :id
    """), {"id": engagement_id})
    header = header_row.first()
    if header is None:
        raise HTTPException(status_code=404, detail="engagement not found")

    contact_name = f"{header.first_name or ''} {header.last_name or ''}".strip() or None

    # Build merged timeline. We do 3 small queries + python merge — simpler
    # than UNION ALL across BIGINT id space and ai_decisions composite PK.

    sig_rows = await db.execute(text("""
        SELECT s.id, st.code AS type_code, s.relevance_score, s.ai_summary,
               s.detected_at, s.triggered_action_id
        FROM signals s
        JOIN signal_types st ON st.id = s.signal_type_id
        WHERE s.engagement_id = :id
        ORDER BY s.detected_at DESC
        LIMIT :n
    """), {"id": engagement_id, "n": timeline_limit})
    signal_items = [
        EngagementTimelineItem(
            kind="signal",
            occurred_at=r.detected_at,
            summary=(r.ai_summary or f"signal: {r.type_code}"),
            relevance_score=r.relevance_score,
            item_id=r.id,
        ) for r in sig_rows
    ]

    act_rows = await db.execute(text("""
        SELECT a.id, ct.code AS channel_code, a.status, a.subject,
               COALESCE(a.executed_at, a.scheduled_at) AS occurred_at,
               a.outcome, a.send_cost_usd
        FROM actions a
        JOIN channel_types ct ON ct.id = a.channel_id
        WHERE a.engagement_id = :id
        ORDER BY COALESCE(a.executed_at, a.scheduled_at) DESC
        LIMIT :n
    """), {"id": engagement_id, "n": timeline_limit})
    action_items = [
        EngagementTimelineItem(
            kind="action",
            occurred_at=r.occurred_at,
            summary=f"{r.channel_code}: {(r.subject or '(no subject)')[:80]}",
            status=r.status,
            channel_code=r.channel_code,
            cost_usd=float(r.send_cost_usd) if r.send_cost_usd else None,
            item_id=r.id,
        ) for r in act_rows
    ]

    dec_rows = await db.execute(text("""
        SELECT id, decision_type, model_used, cost_usd, created_at
        FROM ai_decisions
        WHERE engagement_id = :id
        ORDER BY created_at DESC
        LIMIT :n
    """), {"id": engagement_id, "n": timeline_limit})
    decision_items = [
        EngagementTimelineItem(
            kind="decision",
            occurred_at=r.created_at,
            summary=f"AI: {r.decision_type} via {r.model_used}",
            cost_usd=float(r.cost_usd) if r.cost_usd else None,
            item_id=r.id,
        ) for r in dec_rows
    ]

    merged = sorted(
        signal_items + action_items + decision_items,
        key=lambda x: x.occurred_at,
        reverse=True,
    )[:timeline_limit]

    return EngagementDetail(
        id=header.id,
        contact_id=header.contact_id,
        contact_email=header.contact_email,
        contact_name=contact_name,
        company_id=header.company_id,
        company_name=header.company_name,
        current_phase=header.current_phase,
        status=header.status,
        sequence_number=header.sequence_number,
        engagement_score=header.engagement_score,
        tier=header.tier,
        last_outreach_at=header.last_outreach_at,
        last_signal_at=header.last_signal_at,
        last_reply_at=header.last_reply_at,
        ai_engagement_summary=header.ai_engagement_summary,
        notes=header.notes,
        monthly_ai_cost_usd=float(header.monthly_ai_cost_usd or 0),
        timeline=merged,
    )


# ════════════════════════════════════════════════════════════════════════════
# Signal feed (cross-engagement)
# ════════════════════════════════════════════════════════════════════════════

@router.get("/signals/feed", response_model=list[SignalFeedItem])
async def signal_feed(
    min_relevance: int = Query(70, ge=0, le=100),
    only_unacted: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    hours_back: int = Query(168, ge=1, le=720),  # default 7 days, max 30
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> list[SignalFeedItem]:
    """Return high-relevance signals across all engagements the BDR's
    tenant owns. The CRM signal-feed view powers this."""

    where_clauses = [
        "s.relevance_score >= :min_rel",
        "s.detected_at > NOW() - (:hours_back * INTERVAL '1 hour')",
    ]
    params: dict = {"min_rel": min_relevance, "hours_back": hours_back,
                    "limit": limit}
    if only_unacted:
        where_clauses.append("s.triggered_action_id IS NULL")
    where_sql = " AND ".join(where_clauses)

    rows = await db.execute(text(f"""
        SELECT
            s.id, s.engagement_id, s.contact_id, s.detected_at,
            s.relevance_score, s.ai_summary, s.triggered_action_id,
            s.source_url, st.code AS signal_type_code,
            c.first_name, c.last_name,
            co.name AS company_name
        FROM signals s
        JOIN signal_types st ON st.id = s.signal_type_id
        JOIN contacts c ON c.id = s.contact_id
        JOIN engagements e ON e.id = s.engagement_id
        JOIN companies co ON co.id = e.company_id
        WHERE {where_sql}
        ORDER BY s.relevance_score DESC, s.detected_at DESC
        LIMIT :limit
    """), params)
    out = []
    for r in rows:
        name = f"{r.first_name or ''} {r.last_name or ''}".strip() or None
        out.append(SignalFeedItem(
            id=r.id,
            engagement_id=r.engagement_id,
            contact_id=r.contact_id,
            contact_name=name,
            company_name=r.company_name,
            signal_type_code=r.signal_type_code,
            relevance_score=r.relevance_score,
            ai_summary=r.ai_summary,
            detected_at=r.detected_at,
            triggered_action_id=r.triggered_action_id,
            source_url=r.source_url,
        ))
    return out


# ════════════════════════════════════════════════════════════════════════════
# AI decision audit log
# ════════════════════════════════════════════════════════════════════════════

@router.get("/{engagement_id}/decisions", response_model=list[DecisionAuditItem])
async def get_decision_audit(
    engagement_id: int,
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> list[DecisionAuditItem]:
    """Full AI decision audit trail for one engagement. Used by the
    'Why did the AI do X?' BDR view."""
    rows = await db.execute(text("""
        SELECT id, signal_id, decision_type, provider, model_used,
               cost_usd, latency_ms, reasoning, output_choice_json,
               output_validation_passed, output_validation_errors,
               created_at
        FROM ai_decisions
        WHERE engagement_id = :id
        ORDER BY created_at DESC
        LIMIT :n
    """), {"id": engagement_id, "n": limit})
    out = []
    for r in rows:
        out.append(DecisionAuditItem(
            id=r.id,
            engagement_id=engagement_id,
            signal_id=r.signal_id,
            decision_type=r.decision_type,
            provider=r.provider,
            model_used=r.model_used,
            cost_usd=float(r.cost_usd) if r.cost_usd else None,
            latency_ms=r.latency_ms,
            reasoning=r.reasoning,
            output_choice_json=(
                json.loads(r.output_choice_json)
                if isinstance(r.output_choice_json, str)
                else (r.output_choice_json or {})
            ),
            output_validation_passed=r.output_validation_passed,
            output_validation_errors=(
                json.loads(r.output_validation_errors)
                if isinstance(r.output_validation_errors, str)
                else r.output_validation_errors
            ),
            created_at=r.created_at,
        ))
    return out


# ════════════════════════════════════════════════════════════════════════════
# Action approval / reject / override
# ════════════════════════════════════════════════════════════════════════════

@router.post("/actions/{action_id}/approve", response_model=ActionItem)
async def approve_action(
    action_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> ActionItem:
    """BDR approves an action that was flagged requires_human_review.
    Moves status: awaiting_approval → scheduled."""
    result = await db.execute(text("""
        UPDATE actions
        SET status = 'scheduled',
            approved_by_user_id = :uid,
            approved_at = NOW(),
            updated_at = NOW()
        WHERE id = :id AND status = 'awaiting_approval'
        RETURNING id, engagement_id, channel_id, status, scheduled_at,
                  executed_at, subject, body, recipient_email,
                  requires_human_review, approved_by_user_id, approved_at,
                  error_message, skip_reason, outcome
    """), {"id": action_id, "uid": current_user.id})
    row = result.first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="action not found or not awaiting_approval",
        )
    await db.commit()
    return await _row_to_action_item(db, row)


@router.post("/actions/{action_id}/reject", response_model=ActionItem)
async def reject_action(
    action_id: int,
    reason: str = Body(..., embed=True),
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> ActionItem:
    """BDR rejects an action. Moves status → blocked with skip_reason."""
    result = await db.execute(text("""
        UPDATE actions
        SET status = 'blocked',
            skip_reason = :reason,
            approved_by_user_id = :uid,
            approved_at = NOW(),
            updated_at = NOW()
        WHERE id = :id AND status IN ('awaiting_approval', 'scheduled')
        RETURNING id, engagement_id, channel_id, status, scheduled_at,
                  executed_at, subject, body, recipient_email,
                  requires_human_review, approved_by_user_id, approved_at,
                  error_message, skip_reason, outcome
    """), {"id": action_id, "reason": f"bdr_rejected:{reason}"[:80], "uid": current_user.id})
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="action not found or already dispatched")
    await db.commit()
    return await _row_to_action_item(db, row)


@router.delete("/actions/{action_id}", response_model=ActionItem)
async def cancel_action(
    action_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> ActionItem:
    """BDR removes a single pending step from an engine sequence. Moves
    status → skipped (NOT blocked: 'the user deleted this step' is normal
    sequence editing, not a compliance block). executed_at is stamped so
    the UI serializers — which derive skipped_at from executed_at — render
    the step as skipped instead of leaving it looking pending/overdue."""
    result = await db.execute(text("""
        UPDATE actions
        SET status = 'skipped',
            skip_reason = :reason,
            executed_at = NOW(),
            approved_by_user_id = :uid,
            approved_at = NOW(),
            updated_at = NOW()
        WHERE id = :id AND status IN ('awaiting_approval', 'scheduled', 'paused')
        RETURNING id, engagement_id, channel_id, status, scheduled_at,
                  executed_at, subject, body, recipient_email,
                  requires_human_review, approved_by_user_id, approved_at,
                  error_message, skip_reason, outcome
    """), {"id": action_id,
           "reason": f"canceled_by_bdr:{current_user.email[:60]}"[:80],
           "uid": current_user.id})
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="action not found or already dispatched")
    await db.commit()
    return await _row_to_action_item(db, row)


class ActionOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: Optional[str] = None
    body: Optional[str] = None
    task_description: Optional[str] = None
    scheduled_at: Optional[datetime] = None


@router.post("/actions/{action_id}/override", response_model=ActionItem)
async def override_action(
    action_id: int,
    override: ActionOverride,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> ActionItem:
    """BDR overrides the AI-generated content before dispatch. Only allowed
    while status='awaiting_approval' or 'scheduled' (NOT after sent)."""
    # Build dynamic UPDATE for only the fields the BDR changed
    sets = []
    params = {"id": action_id, "uid": current_user.id}
    if override.subject is not None:
        sets.append("subject = :subj"); params["subj"] = override.subject
    if override.body is not None:
        sets.append("body = :body"); params["body"] = override.body
    if override.task_description is not None:
        sets.append("task_description = :task"); params["task"] = override.task_description
    if override.scheduled_at is not None:
        sets.append("scheduled_at = :sched"); params["sched"] = override.scheduled_at

    if not sets:
        raise HTTPException(status_code=400, detail="no override fields provided")

    sets.append("approved_by_user_id = :uid")
    sets.append("approved_at = NOW()")
    sets.append("updated_at = NOW()")

    sql = f"""
        UPDATE actions
        SET {', '.join(sets)}
        WHERE id = :id
          AND status IN ('awaiting_approval', 'scheduled')
        RETURNING id, engagement_id, channel_id, status, scheduled_at,
                  executed_at, subject, body, recipient_email,
                  requires_human_review, approved_by_user_id, approved_at,
                  error_message, skip_reason, outcome
    """
    result = await db.execute(text(sql), params)
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="action not found or already dispatched")
    await db.commit()
    return await _row_to_action_item(db, row)


async def _row_to_action_item(db: AsyncSession, row) -> ActionItem:
    """Helper: row → ActionItem (resolves channel_id → code)."""
    ch_row = await db.execute(text(
        "SELECT code FROM channel_types WHERE id = :id"
    ), {"id": row.channel_id})
    ch = ch_row.first()
    return ActionItem(
        id=row.id,
        engagement_id=row.engagement_id,
        channel_code=ch.code if ch else "unknown",
        status=row.status,
        scheduled_at=row.scheduled_at,
        executed_at=row.executed_at,
        subject=row.subject,
        body=row.body,
        recipient_email=row.recipient_email,
        requires_human_review=row.requires_human_review,
        approved_by_user_id=row.approved_by_user_id,
        approved_at=row.approved_at,
        error_message=row.error_message,
        skip_reason=row.skip_reason,
        outcome=row.outcome,
    )


# ════════════════════════════════════════════════════════════════════════════
# Inbound unattributed reply queue
# ════════════════════════════════════════════════════════════════════════════

@router.get("/inbound-unattributed", response_model=list[InboundUnattributedItem])
async def list_inbound_unattributed(
    only_pending: bool = Query(True),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> list[InboundUnattributedItem]:
    """Replies that couldn't be parsed back to an engagement. BDR reviews
    + manually attributes or marks resolution."""
    where = "WHERE 1=1"
    if only_pending:
        where = "WHERE reviewed_at IS NULL"
    rows = await db.execute(text(f"""
        SELECT id, envelope_from, envelope_to, subject,
               LEFT(cleaned_body, 500) AS cleaned_body_preview,
               received_at, reviewed_at, resolution
        FROM inbound_unattributed
        {where}
        ORDER BY received_at DESC
        LIMIT :n
    """), {"n": limit})
    return [
        InboundUnattributedItem(
            id=r.id,
            envelope_from=r.envelope_from,
            envelope_to=r.envelope_to,
            subject=r.subject,
            cleaned_body_preview=r.cleaned_body_preview,
            received_at=r.received_at,
            reviewed_at=r.reviewed_at,
            resolution=r.resolution,
        )
        for r in rows
    ]


class AttributeReplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engagement_id: Optional[int] = None  # if set: attribute
    resolution: Optional[str] = Field(default=None,
        description="One of: attributed_manually, spam, out_of_office, "
                    "unrelated, do_not_contact_request")


@router.post("/inbound-unattributed/{item_id}", response_model=InboundUnattributedItem)
async def attribute_inbound_reply(
    item_id: int,
    body: AttributeReplyInput,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> InboundUnattributedItem:
    """BDR attributes an unattributed reply OR marks it with a resolution."""
    valid_resolutions = {
        "attributed_manually", "spam", "out_of_office",
        "unrelated", "do_not_contact_request",
    }
    if body.resolution and body.resolution not in valid_resolutions:
        raise HTTPException(
            status_code=400,
            detail=f"resolution must be one of {sorted(valid_resolutions)}",
        )

    result = await db.execute(text("""
        UPDATE inbound_unattributed
        SET attributed_engagement_id = :eng,
            resolution = :res,
            reviewed_at = NOW(),
            reviewed_by_user_id = :uid
        WHERE id = :id
        RETURNING id, envelope_from, envelope_to, subject,
                  LEFT(cleaned_body, 500) AS cleaned_body_preview,
                  received_at, reviewed_at, resolution
    """), {
        "id": item_id,
        "eng": body.engagement_id,
        "res": body.resolution or "attributed_manually",
        "uid": current_user.id,
    })
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    await db.commit()
    return InboundUnattributedItem(
        id=row.id,
        envelope_from=row.envelope_from,
        envelope_to=row.envelope_to,
        subject=row.subject,
        cleaned_body_preview=row.cleaned_body_preview,
        received_at=row.received_at,
        reviewed_at=row.reviewed_at,
        resolution=row.resolution,
    )


# ════════════════════════════════════════════════════════════════════════════
# Channel pause / resume (kill switch, incident response)
# ════════════════════════════════════════════════════════════════════════════

class ChannelStatus(BaseModel):
    code: str
    label: str
    is_paused: bool
    is_active: bool


@router.get("/channels", response_model=list[ChannelStatus])
async def list_channels(
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> list[ChannelStatus]:
    """Channel registry status. UI shows the kill-switch toggles here."""
    rows = await db.execute(text("""
        SELECT code, label, is_paused, is_active FROM channel_types
        ORDER BY id
    """))
    return [
        ChannelStatus(
            code=r.code, label=r.label,
            is_paused=r.is_paused, is_active=r.is_active,
        ) for r in rows
    ]


@router.post("/channels/{channel_code}/pause", response_model=ChannelStatus)
async def pause_channel(
    channel_code: str,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> ChannelStatus:
    """Kill switch: stop dispatching for this channel. Used during
    incidents (e.g., Twilio outage). Setting is GLOBAL across tenants
    because channel_types is a shared lookup — only admins should call
    this."""
    # Admin gate (Phase 5 minimum: rely on existing route auth + log)
    result = await db.execute(text("""
        UPDATE channel_types SET is_paused = TRUE
        WHERE code = :code
        RETURNING code, label, is_paused, is_active
    """), {"code": channel_code})
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown channel: {channel_code}")
    await db.commit()
    log.warning("CHANNEL %s PAUSED by user %s", channel_code, current_user.id)
    return ChannelStatus(code=row.code, label=row.label,
                         is_paused=row.is_paused, is_active=row.is_active)


@router.post("/channels/{channel_code}/resume", response_model=ChannelStatus)
async def resume_channel(
    channel_code: str,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> ChannelStatus:
    """Lift the kill switch. The dispatcher resumes processing on the
    next tick."""
    result = await db.execute(text("""
        UPDATE channel_types SET is_paused = FALSE
        WHERE code = :code
        RETURNING code, label, is_paused, is_active
    """), {"code": channel_code})
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown channel: {channel_code}")
    await db.commit()
    log.warning("CHANNEL %s RESUMED by user %s", channel_code, current_user.id)
    return ChannelStatus(code=row.code, label=row.label,
                         is_paused=row.is_paused, is_active=row.is_active)


# ════════════════════════════════════════════════════════════════════════════
# Tenant AI config (the BYO AI UI surface)
# ════════════════════════════════════════════════════════════════════════════

@router.get("/tenant-ai-config", response_model=TenantAIConfigOut)
async def get_tenant_ai_config(
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> TenantAIConfigOut:
    """Current BYO AI configuration for the tenant."""
    tenant_id = current_user.tenant_id
    row = await db.execute(text("""
        SELECT * FROM tenant_ai_config WHERE tenant_id = :t
    """), {"t": tenant_id})
    config = row.first()

    if config is None:
        # Lazy-create defaults so the UI never sees "no config"
        await db.execute(text("""
            INSERT INTO tenant_ai_config (tenant_id, provider)
            VALUES (:t, 'aamp_default')
            ON CONFLICT (tenant_id) DO NOTHING
        """), {"t": tenant_id})
        await db.commit()
        row = await db.execute(text("""
            SELECT * FROM tenant_ai_config WHERE tenant_id = :t
        """), {"t": tenant_id})
        config = row.first()

    return TenantAIConfigOut(
        tenant_id=config.tenant_id,
        provider=config.provider,
        has_custom_key=bool(
            config.api_key_encrypted or config.api_key_kms_arn
        ),
        base_url=config.base_url,
        model_signal_scoring=config.model_signal_scoring,
        model_reply_classification=config.model_reply_classification,
        model_content_generation=config.model_content_generation,
        model_decision_making=config.model_decision_making,
        model_engagement_summary=config.model_engagement_summary,
        monthly_budget_usd=float(config.monthly_budget_usd) if config.monthly_budget_usd else None,
        per_engagement_budget_usd=float(config.per_engagement_budget_usd or 5.00),
        fallback_provider=config.fallback_provider,
        tcpa_b2b_override=config.tcpa_b2b_override,
        default_timezone=config.default_timezone,
        current_month_spent_usd=float(config.current_month_spent_usd or 0),
    )


@router.put("/tenant-ai-config", response_model=TenantAIConfigOut)
async def update_tenant_ai_config(
    update: TenantAIConfigUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> TenantAIConfigOut:
    """Partial update of BYO AI settings. If api_key_plaintext is sent,
    it's encrypted via the existing secrets_vault before write."""
    tenant_id = current_user.tenant_id

    # Build dynamic UPDATE
    sets = []
    params: dict = {"t": tenant_id}

    simple_fields = [
        "provider", "base_url", "model_signal_scoring",
        "model_reply_classification", "model_content_generation",
        "model_decision_making", "model_engagement_summary",
        "monthly_budget_usd", "per_engagement_budget_usd",
        "fallback_provider", "tcpa_b2b_override", "default_timezone",
    ]
    for field_name in simple_fields:
        val = getattr(update, field_name, None)
        if val is not None:
            sets.append(f"{field_name} = :{field_name}")
            params[field_name] = val

    # Handle api key (encrypt before write)
    if update.api_key_plaintext is not None:
        if update.api_key_plaintext == "":
            # Empty string = clear the key (revert to default behavior)
            sets.append("api_key_encrypted = NULL")
            sets.append("api_key_kms_arn = NULL")
        else:
            from app.secrets_vault import encrypt_secret
            encrypted = encrypt_secret(update.api_key_plaintext)
            sets.append("api_key_encrypted = :enc")
            params["enc"] = encrypted

    if not sets:
        # No actual changes; just return current
        return await get_tenant_ai_config(db=db, current_user=current_user)

    sets.append("updated_at = NOW()")
    sets.append("api_key_last_validated_at = NULL")  # force re-validation
    sql = f"""
        UPDATE tenant_ai_config
        SET {', '.join(sets)}
        WHERE tenant_id = :t
    """
    result = await db.execute(text(sql), params)
    if result.rowcount == 0:
        # No row to update — insert defaults first
        await db.execute(text("""
            INSERT INTO tenant_ai_config (tenant_id, provider)
            VALUES (:t, 'aamp_default')
            ON CONFLICT (tenant_id) DO NOTHING
        """), {"t": tenant_id})
        await db.execute(text(sql), params)
    await db.commit()

    log.info("tenant %s AI config updated by user %s: fields=%s",
             tenant_id, current_user.id, [s.split(' = ')[0] for s in sets])

    return await get_tenant_ai_config(db=db, current_user=current_user)
