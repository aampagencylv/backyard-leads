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

from app.database import get_db
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
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Materialize the 30-day default template into queued steps for this contact.
    Refuses if a non-paused 'main' sequence already exists — use pause+resume or
    cancel first to avoid duplicates."""
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Idempotency check: already has unsent main-sequence steps
    existing = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.contact_id == contact_id,
            GeneratedEmail.sequence_label == label,
            GeneratedEmail.is_sent == False,
            GeneratedEmail.skipped_at.is_(None),
        ).limit(1)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail=f"Active '{label}' sequence already exists for this contact")

    created = await start_sequence_from_template(db, contact, sequence_label=label)
    return {"created": created, "template_steps": len(DEFAULT_30DAY_TEMPLATE)}


@router.get("/contact/{contact_id}")
async def get_for_contact(
    contact_id: int,
    label: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List sequence steps for a contact, ordered by sequence_order. Optionally
    filter by label ('main', 'post_call'). Returns metadata the UI needs to
    render the read-only sequence card."""
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

    return [
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
        }
        for s in rows
    ]


@router.post("/pause/{contact_id}")
async def pause(
    contact_id: int,
    label: str = Query("main"),
    reason: str = Query("manual"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    n = await pause_sequence(db, contact_id, reason=f"manual ({user.email}): {reason}", sequence_label=label)
    await db.commit()
    return {"paused": n}


@router.post("/resume/{contact_id}")
async def resume(
    contact_id: int,
    label: str = Query("main"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    n = await resume_sequence(db, contact_id, sequence_label=label)
    await db.commit()
    return {"resumed": n}


@router.post("/run-now")
async def run_now(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Admin: force a scheduler tick immediately. Useful for testing — don't
    have to wait 60s for the next loop iteration."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    counters = await process_pending_steps(db)
    return counters


# ============================================================
# Post-call sequence — Claude-drafted 3-step follow-up using transcript
# ============================================================

@router.post("/post-call/{activity_id}")
async def trigger_post_call_sequence(
    activity_id: int,
    db: AsyncSession = Depends(get_db),
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
    try:
        steps = await generate_post_call_sequence(
            business_name=company.name,
            business_type=company.business_type or company.industry or "backyard professional",
            contact_name=contact.full_name,
            transcript=activity.transcript,
            summary=activity.call_summary,
            duration_seconds=activity.call_duration_seconds,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI generation failed: {e}")

    # The thank-you email goes out ~2 hours after this trigger so it arrives
    # while the conversation is still fresh but not so soon that it feels
    # automated / weird.
    base_time = datetime.now(timezone.utc) + timedelta(hours=2)

    skip_map = {
        "email":    ["no_email", "opted_out"],
        "imessage": ["no_phone", "opted_out", "landline"],
    }

    created = 0
    for idx, s in enumerate(steps, start=1):
        scheduled = base_time + timedelta(days=s["day"])
        ge = GeneratedEmail(
            contact_id=contact.id,
            company_id=company.id,
            step_type=s["step_type"],
            email_type=f"post_call_{idx}",
            subject=s.get("subject") or f"post-call step {idx}",
            body=s.get("body") or "",
            sequence_order=idx,
            send_delay_days=s["day"],
            scheduled_send_at=scheduled,
            skip_if_json=json.dumps(skip_map.get(s["step_type"], [])),
            auto_execute=True,  # post-call steps are all auto (email + imessage)
            sequence_label="post_call",
        )
        db.add(ge)
        created += 1

    db.add(Activity(
        company_id=company.id, contact_id=contact.id, user_id=user.id,
        activity_type="sequence_created",
        content=f"[Post-call] {created}-step follow-up sequence queued from call transcript",
    ))
    await db.commit()
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
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Reorder steps within a sequence. Only renumbers sequence_order — does
    NOT touch scheduled_send_at (intentional: dragging a step doesn't auto-shift
    its date). Use /api/sequences/reschedule/{step_id} for date changes."""
    rows = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.contact_id == req.contact_id,
            GeneratedEmail.sequence_label == req.sequence_label,
        )
    )).scalars().all()
    by_id = {r.id: r for r in rows}
    seen_ids = set()
    for idx, step_id in enumerate(req.ordered_step_ids, start=1):
        step = by_id.get(step_id)
        if not step:
            raise HTTPException(status_code=400, detail=f"Step {step_id} not in this sequence")
        if step_id in seen_ids:
            raise HTTPException(status_code=400, detail=f"Step {step_id} listed twice")
        seen_ids.add(step_id)
        step.sequence_order = idx
    await db.commit()
    return {"reordered": len(req.ordered_step_ids)}
