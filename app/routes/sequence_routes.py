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
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import User, Contact, GeneratedEmail
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
