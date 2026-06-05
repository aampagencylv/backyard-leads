"""
Sequence engine HTTP surface.

  POST /api/sequences/start/{contact_id}      — start the 30-day default
  GET  /api/sequences/contact/{contact_id}    — list steps for a contact
  POST /api/sequences/pause/{contact_id}      — pause remaining steps
  POST /api/sequences/resume/{contact_id}     — un-pause + re-anchor schedule
  POST /api/sequences/run-now                 — admin: force engine tick
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.tenancy import get_tenant_db
from app.models import User, Contact, Company, Activity, GeneratedEmail
from app.auth import get_current_user
from app.services.sequence_engine import (
    DEFAULT_30DAY_TEMPLATE,
    start_sequence_from_template,
    pause_sequence,
    resume_sequence,
    process_pending_steps,
)


router = APIRouter(prefix="/api/sequences", tags=["sequences"])


@router.post("/start/{contact_id}")
async def start(
    contact_id: int,
    label: str = Query("main"),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Materialize the 30-day default template into queued steps for this contact.

    Idempotent: any existing unsent + non-skipped steps under the same label
    are deleted first, then the new template is created. Calling this twice
    in a row produces one sequence (not two), which is what callers expect —
    'start' means 'reset to fresh template', not 'append'. SENT steps stay on
    the timeline as historical record.
    """
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Engagement engine: terminate any active engagement on this contact
    # then start a fresh one. This is the "reset to template" semantics
    # the BDR expects when they click Start Sequence.
    from app.engagement_engine.lifecycle import (
        start_engagement, terminate_engagement,
    )
    deleted = 0
    try:
        deleted = await terminate_engagement(
            db, contact_id, reason="manual_restart_by_bdr",
        )
    except Exception:
        deleted = 0
    created = await start_engagement(
        db, contact, sequence_label=label,
        initiated_by=f"manual:{user.email[:24]}",
    )
    return {"created": created, "deleted_pending": deleted, "template_steps": len(DEFAULT_30DAY_TEMPLATE)}


@router.get("/contact/{contact_id}")
async def get_for_contact(
    contact_id: int,
    label: Optional[str] = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """List sequence steps for a contact, ordered by sequence_order. Returns
    the UNION of legacy GeneratedEmail rows (sent history + any leftover
    pre-cutover pending) and new-engine actions (the live sequence).

    The UI doesn't care which table a row came from — both shapes are
    flattened to the same dict so the sequence card renders unchanged."""
    from sqlalchemy import text as _sa_text

    # Legacy GeneratedEmail rows — these are now mostly historical
    # (sent emails) but we keep them so the timeline view stays intact.
    q = select(GeneratedEmail).where(GeneratedEmail.contact_id == contact_id)
    if label:
        q = q.where(GeneratedEmail.sequence_label == label)
    q = q.order_by(GeneratedEmail.sequence_label, GeneratedEmail.sequence_order)
    rows = (await db.execute(q)).scalars().all()

    def status(s: GeneratedEmail) -> str:
        if s.is_sent: return "sent"
        if s.skipped_at: return "skipped"
        if s.paused_at: return "paused"
        return "pending"

    out = [
        {
            "id": s.id,
            "sequence_label": s.sequence_label or "main",
            "sequence_order": s.sequence_order,
            "step_type": s.step_type,
            "label": s.email_type,
            "subject": s.subject,
            "body": s.body,
            "send_delay_days": s.send_delay_days,
            "scheduled_send_at": s.scheduled_send_at.isoformat() if s.scheduled_send_at else None,
            "sent_at": s.sent_at.isoformat() if s.sent_at else None,
            "skipped_at": s.skipped_at.isoformat() if s.skipped_at else None,
            "skip_reason": s.skip_reason,
            "skip_if": json.loads(s.skip_if_json) if s.skip_if_json else [],
            "paused_at": s.paused_at.isoformat() if s.paused_at else None,
            "auto_execute": bool(s.auto_execute),
            "task_id": s.task_id,
            "status": status(s),
            "source": "legacy",
        }
        for s in rows
    ]

    # New-engine actions for the contact. Channel code resolved via a join.
    action_rows = (await db.execute(_sa_text("""
        SELECT a.id, a.channel_id, ct.code AS channel_code,
               a.subject, a.body, a.scheduled_at, a.executed_at,
               a.status, a.skip_reason, a.engagement_id,
               ROW_NUMBER() OVER (PARTITION BY a.engagement_id
                                  ORDER BY a.scheduled_at) AS step_order
        FROM actions a
        JOIN channel_types ct ON ct.id = a.channel_id
        WHERE a.contact_id = :c
        ORDER BY a.scheduled_at
    """), {"c": contact_id})).fetchall()

    def _action_status(r) -> str:
        if r.status == "sent" or r.executed_at is not None:
            return "sent"
        if r.status == "skipped":
            return "skipped"
        if r.status == "paused":
            return "paused"
        return "pending"

    def _step_type_from_channel(code: str) -> str:
        return {
            "email": "email",
            "sms": "imessage",
            "call_task": "call",
            "linkedin": "linkedin",
            "manual": "manual",
            "wait": "wait",
        }.get(code, code)

    for r in action_rows:
        out.append({
            "id": int(r.id),
            "sequence_label": "main",
            "sequence_order": int(r.step_order),
            "step_type": _step_type_from_channel(r.channel_code),
            "label": None,
            "subject": r.subject,
            "body": r.body,
            "send_delay_days": None,
            "scheduled_send_at": r.scheduled_at.isoformat() if r.scheduled_at else None,
            "sent_at": r.executed_at.isoformat() if r.executed_at else None,
            "skipped_at": None,
            "skip_reason": r.skip_reason,
            "skip_if": [],
            "paused_at": None,
            "auto_execute": r.channel_code in ("email", "sms"),
            "task_id": None,
            "status": _action_status(r),
            "source": "engagement_engine",
            "engagement_id": int(r.engagement_id),
        })

    return out


@router.post("/pause/{contact_id}")
async def pause(
    contact_id: int,
    label: str = Query("main"),
    reason: str = Query("manual"),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    from app.engagement_engine.lifecycle import pause_engagement
    n = await pause_engagement(
        db, contact_id, reason=f"manual ({user.email}): {reason}",
    )
    return {"paused": n}


class RescheduleRequest(BaseModel):
    resume_at: Optional[str] = None  # ISO date string, e.g. "2026-07-15"


@router.post("/resume/{contact_id}")
async def resume(
    contact_id: int,
    label: str = Query("main"),
    body: Optional[RescheduleRequest] = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    resume_at_dt = None
    if body and body.resume_at:
        try:
            parsed = datetime.fromisoformat(body.resume_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            resume_at_dt = parsed
        except (ValueError, TypeError):
            pass
    from app.engagement_engine.lifecycle import resume_engagement
    n = await resume_engagement(db, contact_id, resume_at=resume_at_dt)
    return {"resumed": n, "resume_at": resume_at_dt.isoformat() if resume_at_dt else "now"}


class ReworkRequest(BaseModel):
    call_notes: str
    activity_id: Optional[int] = None  # If provided, pulls transcript/summary from this call


@router.post("/rework/{contact_id}")
async def rework_sequence(
    contact_id: int,
    body: ReworkRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Delete remaining unsent steps and regenerate them based on call context.
    The BDR provides call notes; if an activity_id is given, the transcript
    and AI summary from that call are also fed to the generator."""
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    company = (await db.execute(select(Company).where(Company.id == contact.company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    transcript = None
    summary = None
    if body.activity_id:
        act = (await db.execute(select(Activity).where(Activity.id == body.activity_id))).scalar_one_or_none()
        if act:
            transcript = act.transcript
            summary = act.call_summary

    # Count and delete remaining unsent steps
    remaining = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.contact_id == contact_id,
            GeneratedEmail.sequence_label == "main",
            GeneratedEmail.is_sent == False,
            GeneratedEmail.skipped_at.is_(None),
        ).order_by(GeneratedEmail.sequence_order)
    )).scalars().all()

    deleted = len(remaining)
    # Find the highest sent step's order to continue numbering from
    last_sent = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.contact_id == contact_id,
            GeneratedEmail.sequence_label == "main",
            GeneratedEmail.is_sent == True,
        ).order_by(GeneratedEmail.sequence_order.desc())
    )).scalars().first()
    start_order = (last_sent.sequence_order + 1) if last_sent else 1

    for r in remaining:
        await db.delete(r)
    await db.flush()

    # Generate new steps
    from app.services.email_generator import generate_reworked_sequence
    from app.runtime_config import get_messaging_direction
    direction = await get_messaging_direction(db)

    steps = await generate_reworked_sequence(
        business_name=company.name,
        business_type=company.business_type or company.industry or "backyard professional",
        contact_name=contact.full_name,
        call_notes=body.call_notes,
        transcript=transcript,
        summary=summary,
        remaining_step_count=min(deleted, 7) or 5,
        messaging_direction=direction,
    )

    # Append the AI-reworked steps to the contact's active engagement.
    # The legacy GeneratedEmail rows we deleted above were already
    # paused/pending and won't fire; the new actions are what the engine
    # will dispatch from here forward.
    from app.engagement_engine.lifecycle import append_steps_to_engagement
    payload_steps = []
    for s in steps:
        stype = s["step_type"]
        skip_map = {
            "email": ["no_email", "opted_out"],
            "imessage": ["no_phone", "opted_out", "landline"],
            "call": ["no_phone"],
            "linkedin": ["no_linkedin"],
        }
        payload_steps.append({
            "day": s["day"],
            "step_type": stype,
            "subject": s.get("subject", "follow-up"),
            "body": s.get("body", ""),
            "label": f"rework_{stype}",
            "skip_if": skip_map.get(stype, []),
        })
    created = await append_steps_to_engagement(
        db, contact, payload_steps, strategy_tag="post_call_rework",
    )

    db.add(Activity(
        company_id=company.id, contact_id=contact.id, user_id=user.id,
        activity_type="sequence_reworked",
        content=f"Sequence reworked after call — {deleted} old steps removed, {created} new steps generated from call context",
    ))
    await db.commit()

    return {"deleted": deleted, "created": created}


@router.post("/run-now")
async def run_now(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """super_admin only: force one engagement-engine dispatcher tick
    immediately. run_dispatcher_tick CLAIMS DUE ACTIONS ACROSS EVERY
    TENANT — it's the per-minute cron worker, not a per-tenant action.
    Restricted to super_admin to prevent a tenant-scoped admin from
    accidentally dispatching another tenant's outbound on demand."""
    if user.role != "super_admin":
        raise HTTPException(
            status_code=403,
            detail="super_admin only — this endpoint dispatches actions across every tenant. Per-tenant testing should call /api/integrations/sidebar/send-next-step instead, which bumps a single tenant-scoped action and lets the cron tick send it.",
        )
    from app.engagement_engine.dispatcher import run_dispatcher_tick
    report = await run_dispatcher_tick()
    return {
        "fetched": report.fetched,
        "sent": report.sent,
        "failed": report.failed,
        "blocked": report.blocked,
        "transient_rescheduled": report.transient_rescheduled,
        "duration_ms": report.duration_ms,
    }


# ============================================================
# Post-call sequence — Claude-drafted 3-step follow-up using transcript
# ============================================================

@router.post("/post-call/{activity_id}")
async def trigger_post_call_sequence(
    activity_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Generate a 3-step post-call follow-up sequence using the call transcript +
    AI summary. Creates GeneratedEmail rows under sequence_label='post_call' so
    they don't interleave with the main 30-day cadence on the timeline.

    Day 0 (sent ~2 hours after this trigger): thank-you email referencing
                                              specific points from the call.
    Day 2: iMessage bump if no response.
    Day 5: calendar nudge with 2-3 specific time options.
    """
    activity = (await db.execute(select(Activity).where(Activity.id == activity_id))).scalar_one_or_none()
    if not activity:
        raise HTTPException(status_code=404, detail="Call activity not found")
    if activity.activity_type != "call":
        raise HTTPException(status_code=400, detail="This activity is not a call")
    if not activity.contact_id:
        raise HTTPException(status_code=400, detail="Call has no contact attached — can't sequence to nobody")
    if not activity.transcript:
        raise HTTPException(status_code=400, detail="Transcript not ready yet. Wait for it to finish processing or click 🔄 Transcribe.")

    contact = (await db.execute(select(Contact).where(Contact.id == activity.contact_id))).scalar_one_or_none()
    company = (await db.execute(select(Company).where(Company.id == activity.company_id))).scalar_one_or_none()
    if not contact or not company:
        raise HTTPException(status_code=404, detail="Contact or company missing")

    # Refuse if a post_call sequence is already active for this contact — avoid
    # accidentally triggering twice from the same call. They can pause+restart.
    existing = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.contact_id == contact.id,
            GeneratedEmail.sequence_label == "post_call",
            GeneratedEmail.is_sent == False,
            GeneratedEmail.skipped_at.is_(None),
            GeneratedEmail.paused_at.is_(None),
        ).limit(1)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="A post-call sequence is already active for this contact. Pause it first if you want to restart.")

    from app.services.email_generator import generate_post_call_sequence
    from app.runtime_config import get_messaging_direction
    direction = await get_messaging_direction(db)
    try:
        steps = await generate_post_call_sequence(
            business_name=company.name,
            business_type=company.business_type or company.industry or "backyard professional",
            contact_name=contact.full_name,
            transcript=activity.transcript,
            summary=activity.call_summary,
            duration_seconds=activity.call_duration_seconds,
            messaging_direction=direction,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI generation failed: {e}")

    # Append post-call steps to the contact's active engagement. The
    # thank-you fires +2 hours after this call so it lands while the
    # conversation's still fresh but not so soon it feels robotic. We
    # pass offset_hours=2 to append_steps_to_engagement which shifts
    # every step's scheduled_at by that amount in addition to its `day`
    # offset (the legacy code used `base_time = now + timedelta(hours=2)`
    # as the anchor — same behavior, expressed through the engine API).
    from app.engagement_engine.lifecycle import append_steps_to_engagement
    payload_steps = []
    for s in steps:
        skip_map = {
            "email":    ["no_email", "opted_out"],
            "imessage": ["no_phone", "opted_out", "landline"],
        }
        payload_steps.append({
            "day": s["day"],
            "step_type": s["step_type"],
            "subject": s.get("subject") or "Post-call follow-up",
            "body": s.get("body") or "",
            "label": f"post_call_{s['step_type']}",
            "skip_if": skip_map.get(s["step_type"], []),
        })
    created = await append_steps_to_engagement(
        db, contact, payload_steps,
        strategy_tag="post_call",
        offset_hours=2.0,
        sequence_label_hint="post_call",
    )

    db.add(Activity(
        company_id=company.id, contact_id=contact.id, user_id=user.id,
        activity_type="sequence_created",
        content=f"[Post-call] {created}-step follow-up sequence queued from call transcript",
    ))
    await db.commit()

    try:
        from app.services.webhook_dispatch import dispatch_event
        await dispatch_event(db, "sequence.created", {
            "contact_id": contact.id,
            "company_id": company.id,
            "company_name": company.name,
            "step_count": created,
            "first_send_at": base_time.isoformat(),
            "kind": "post_call",
        })
    except Exception:
        pass

    return {
        "created": created,
        "first_send_at": base_time.isoformat(),
        "steps": [{"day": s["day"], "step_type": s["step_type"], "subject": s.get("subject")} for s in steps],
    }


# ============================================================
# Drag-and-drop step reordering
# ============================================================

class ReorderRequest(BaseModel):
    contact_id: int
    sequence_label: str = "main"
    ordered_step_ids: list[int]  # sequence_order will be 1, 2, 3, ... in this order


@router.patch("/reorder")
async def reorder(
    req: ReorderRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Reorder steps within a sequence. The GET /api/sequences/contact/{id}
    endpoint returns a UNION of legacy GeneratedEmail rows and new-engine
    `actions` rows, so the IDs in `ordered_step_ids` may be from either
    namespace. We split them:

    - Legacy GeneratedEmail rows → renumber sequence_order in place
      (the legacy semantic: drag changes display order, not scheduled_send_at)
    - New-engine actions → reflow scheduled_at to enforce the new order
      (actions have no sequence_order column; their order IS scheduled_at,
      so reorder MUST shift dates to preserve the new sequence)

    Mixed lists are handled: legacy IDs renumber, action IDs reflow.
    Duplicates and unknown IDs are silently skipped (no 400 — the frontend
    sends action IDs that won't appear in the legacy query, and that's
    expected post-cutover)."""
    from sqlalchemy import text as _sa_text
    legacy_rows = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.contact_id == req.contact_id,
            GeneratedEmail.sequence_label == req.sequence_label,
        )
    )).scalars().all()
    legacy_by_id = {r.id: r for r in legacy_rows}

    # Pull this contact's actions ordered by current scheduled_at so we
    # know which IDs are in the action namespace and what their current
    # times are — we'll redistribute those times in the new order.
    action_rows = (await db.execute(_sa_text("""
        SELECT a.id, a.scheduled_at FROM actions a
        JOIN contacts c ON c.id = a.contact_id
        WHERE a.contact_id = :c
          AND a.tenant_id = c.tenant_id
          AND a.status IN ('scheduled', 'paused', 'awaiting_approval')
        ORDER BY a.scheduled_at ASC, a.id ASC
    """), {"c": req.contact_id})).fetchall()
    action_by_id = {int(r.id): r.scheduled_at for r in action_rows}
    action_times_sorted = sorted(action_by_id.values())

    seen_ids: set[int] = set()
    legacy_renumbered = 0
    actions_reflowed = 0

    # First pass: legacy renumbering
    legacy_order_idx = 0
    for step_id in req.ordered_step_ids:
        if step_id in seen_ids:
            continue
        seen_ids.add(step_id)
        step = legacy_by_id.get(step_id)
        if step is not None:
            legacy_order_idx += 1
            step.sequence_order = legacy_order_idx
            legacy_renumbered += 1

    # Second pass: reflow action scheduled_at by reassigning the existing
    # set of timestamps to the new ID order. This preserves the calendar
    # footprint (no steps newly created or rescheduled into the past) but
    # honors the BDR's drag.
    action_order_idx = 0
    seen_ids.clear()
    for step_id in req.ordered_step_ids:
        if step_id in seen_ids:
            continue
        seen_ids.add(step_id)
        if step_id in action_by_id and action_order_idx < len(action_times_sorted):
            new_time = action_times_sorted[action_order_idx]
            await db.execute(_sa_text("""
                UPDATE actions
                SET scheduled_at = :sched,
                    updated_at = NOW()
                WHERE id = :aid
            """), {"aid": step_id, "sched": new_time})
            action_order_idx += 1
            actions_reflowed += 1

    await db.commit()
    return {
        "reordered": legacy_renumbered + actions_reflowed,
        "legacy_renumbered": legacy_renumbered,
        "actions_reflowed": actions_reflowed,
    }
