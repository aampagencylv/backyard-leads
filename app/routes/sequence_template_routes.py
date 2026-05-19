"""Admin-only sequence template CRUD + apply-to-existing.

Templates live in the sequence_templates table. start_sequence_from_template
reads the row with is_default=True for new sequences. Admins can edit
existing templates, create new ones, switch which is default, and
optionally re-anchor in-flight sequences onto a new template.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import User, SequenceTemplate, GeneratedEmail, Contact, Company, Activity


router = APIRouter(prefix="/api/sequence-templates", tags=["sequence-templates"])


ALLOWED_STEP_TYPES = {"email", "imessage", "call", "linkedin"}
ALLOWED_SKIP_CONDITIONS = {"no_email", "no_phone", "no_linkedin", "opted_out", "landline"}


def _require_admin(user: User) -> None:
    if user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin only")


def _validate_steps(steps: list[dict]) -> list[dict]:
    """Normalize + validate step list. Raises HTTPException on bad input."""
    if not isinstance(steps, list) or not steps:
        raise HTTPException(400, "steps must be a non-empty list")
    cleaned: list[dict] = []
    last_day = -1
    for i, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise HTTPException(400, f"step {i} is not an object")
        day = raw.get("day")
        if not isinstance(day, int) or day < 0:
            raise HTTPException(400, f"step {i}: day must be a non-negative integer")
        if day < last_day:
            raise HTTPException(400, f"step {i}: day {day} comes before earlier step's day {last_day}")
        last_day = day
        st = raw.get("step_type")
        if st not in ALLOWED_STEP_TYPES:
            raise HTTPException(400, f"step {i}: step_type {st!r} not in {sorted(ALLOWED_STEP_TYPES)}")
        label = raw.get("label") or ""
        if not isinstance(label, str) or len(label) > 60:
            raise HTTPException(400, f"step {i}: label must be a string ≤60 chars")
        skip_if = raw.get("skip_if") or []
        if not isinstance(skip_if, list) or any(s not in ALLOWED_SKIP_CONDITIONS for s in skip_if):
            raise HTTPException(400, f"step {i}: skip_if entries must be in {sorted(ALLOWED_SKIP_CONDITIONS)}")
        auto = bool(raw.get("auto", st in {"email", "imessage"}))
        cleaned.append({
            "day": day,
            "step_type": st,
            "label": label,
            "skip_if": skip_if,
            "auto": auto,
        })
    return cleaned


def _to_dict(t: SequenceTemplate) -> dict:
    try:
        steps = json.loads(t.steps_json)
    except (TypeError, ValueError):
        steps = []
    return {
        "id": t.id,
        "name": t.name,
        "is_active": t.is_active,
        "is_default": t.is_default,
        "steps": steps,
        "step_count": len(steps),
        "auto_skip_days": t.auto_skip_days,
        "auto_resume_days": t.auto_resume_days,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


# ============================================================
# CRUD
# ============================================================

@router.get("")
async def list_templates(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    rows = (await db.execute(
        select(SequenceTemplate).order_by(
            SequenceTemplate.is_default.desc(),
            SequenceTemplate.is_active.desc(),
            SequenceTemplate.name,
        )
    )).scalars().all()
    return [_to_dict(t) for t in rows]


@router.get("/{template_id}")
async def get_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    t = (await db.execute(select(SequenceTemplate).where(SequenceTemplate.id == template_id))).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Template not found")
    return _to_dict(t)


class TemplatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    steps: list[dict]
    auto_skip_days: int = 3
    auto_resume_days: int = 0
    is_active: bool = True


@router.post("")
async def create_template(
    payload: TemplatePayload,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    steps = _validate_steps(payload.steps)
    if payload.auto_skip_days < 0 or payload.auto_skip_days > 60:
        raise HTTPException(400, "auto_skip_days must be 0..60")
    if payload.auto_resume_days < 0 or payload.auto_resume_days > 90:
        raise HTTPException(400, "auto_resume_days must be 0..90")
    # Reject duplicate name
    exists = (await db.execute(
        select(SequenceTemplate).where(SequenceTemplate.name == payload.name)
    )).scalar_one_or_none()
    if exists:
        raise HTTPException(400, f"A template named {payload.name!r} already exists")
    t = SequenceTemplate(
        name=payload.name,
        is_active=payload.is_active,
        is_default=False,  # explicit set_default endpoint to flip
        steps_json=json.dumps(steps),
        auto_skip_days=payload.auto_skip_days,
        auto_resume_days=payload.auto_resume_days,
        created_by=user.id,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return _to_dict(t)


@router.patch("/{template_id}")
async def update_template(
    template_id: int,
    payload: TemplatePayload,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    t = (await db.execute(select(SequenceTemplate).where(SequenceTemplate.id == template_id))).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Template not found")
    steps = _validate_steps(payload.steps)
    if payload.auto_skip_days < 0 or payload.auto_skip_days > 60:
        raise HTTPException(400, "auto_skip_days must be 0..60")
    if payload.auto_resume_days < 0 or payload.auto_resume_days > 90:
        raise HTTPException(400, "auto_resume_days must be 0..90")
    if payload.name != t.name:
        dup = (await db.execute(
            select(SequenceTemplate).where(
                SequenceTemplate.name == payload.name,
                SequenceTemplate.id != t.id,
            )
        )).scalar_one_or_none()
        if dup:
            raise HTTPException(400, f"A template named {payload.name!r} already exists")
    t.name = payload.name
    t.steps_json = json.dumps(steps)
    t.auto_skip_days = payload.auto_skip_days
    t.auto_resume_days = payload.auto_resume_days
    t.is_active = payload.is_active
    await db.commit()
    await db.refresh(t)
    return _to_dict(t)


@router.post("/{template_id}/set-default")
async def set_default(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    t = (await db.execute(select(SequenceTemplate).where(SequenceTemplate.id == template_id))).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Template not found")
    if not t.is_active:
        raise HTTPException(400, "Cannot make an inactive template default — activate first")
    # Clear is_default on every other row, set on this one. Single-write per row;
    # cheap given a few rows.
    others = (await db.execute(select(SequenceTemplate).where(SequenceTemplate.id != template_id))).scalars().all()
    for o in others:
        o.is_default = False
    t.is_default = True
    await db.commit()
    return _to_dict(t)


@router.delete("/{template_id}")
async def delete_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    t = (await db.execute(select(SequenceTemplate).where(SequenceTemplate.id == template_id))).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Template not found")
    if t.is_default:
        raise HTTPException(400, "Cannot delete the default template — promote another one first")
    await db.execute(delete(SequenceTemplate).where(SequenceTemplate.id == template_id))
    await db.commit()
    return {"ok": True, "deleted_id": template_id}


# ============================================================
# Apply-to-existing
# ============================================================

class ApplyToExistingRequest(BaseModel):
    company_ids: Optional[list[int]] = None  # None = all companies in sequencing
    dry_run: bool = True


@router.post("/{template_id}/apply-to-existing")
async def apply_to_existing(
    template_id: int,
    payload: ApplyToExistingRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Re-anchor in-flight sequences onto this template.

    What it does, per affected contact:
      1. Find the highest sequence_order already sent.
      2. Delete remaining unsent + non-skipped steps (lossy on draft bodies
         but matches the template's intent — admins are opting in).
      3. Materialize the template steps that come after the last-sent point,
         anchored at now + (template_step.day - last_sent.day_offset) days.

    Paused sequences are NOT touched (paused_at is preserved across the
    delete/re-create). Skipped steps stay skipped.
    """
    _require_admin(user)
    t = (await db.execute(select(SequenceTemplate).where(SequenceTemplate.id == template_id))).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Template not found")
    try:
        template_steps = json.loads(t.steps_json)
    except (TypeError, ValueError):
        raise HTTPException(500, "Template has malformed steps_json")

    # Build the contact list
    q = select(Contact.id, Contact.company_id).join(GeneratedEmail, GeneratedEmail.contact_id == Contact.id)
    if payload.company_ids:
        q = q.where(Contact.company_id.in_(payload.company_ids))
    contact_pairs = list({(cid, coid) for cid, coid in (await db.execute(q.distinct())).all()})

    now = datetime.now(timezone.utc)
    report = {"contacts_considered": len(contact_pairs), "contacts_updated": 0, "steps_deleted": 0, "steps_created": 0}

    if payload.dry_run:
        return report

    for contact_id, company_id in contact_pairs:
        existing = (await db.execute(
            select(GeneratedEmail).where(GeneratedEmail.contact_id == contact_id)
            .order_by(GeneratedEmail.sequence_order)
        )).scalars().all()
        if not existing:
            continue
        last_sent_order = max((e.sequence_order for e in existing if e.is_sent), default=0)
        # Delete remaining unsent, non-paused, non-skipped steps. Preserve
        # paused/skipped rows so resume + skip history stays intact.
        to_delete = [e for e in existing if not e.is_sent and e.paused_at is None and e.skipped_at is None]
        if to_delete:
            await db.execute(
                delete(GeneratedEmail).where(GeneratedEmail.id.in_([e.id for e in to_delete]))
            )
            report["steps_deleted"] += len(to_delete)
        # Materialize template steps with sequence_order > last_sent_order.
        # Anchor day=0 to "now"; future days shift forward by their relative
        # day delta. Skip steps whose order is <= last_sent_order — those
        # rows are kept (sent or paused or skipped).
        next_order = last_sent_order + 1
        anchor_day = template_steps[last_sent_order]["day"] if last_sent_order < len(template_steps) else 0
        for tstep in template_steps[last_sent_order:]:
            day_offset = tstep["day"] - anchor_day
            scheduled = now + timedelta(days=max(day_offset, 0))
            auto = bool(tstep.get("auto", tstep["step_type"] in {"email", "imessage"}))
            ge = GeneratedEmail(
                company_id=company_id,
                contact_id=contact_id,
                sequence_order=next_order,
                step_type=tstep["step_type"],
                email_type=tstep.get("label") or "",
                sequence_label="main",
                skip_if_json=json.dumps(tstep.get("skip_if") or []),
                auto_execute=auto,
                scheduled_send_at=scheduled,
                subject="",  # email steps that get auto-fired will fill in via generator on send
                body="",
            )
            db.add(ge)
            report["steps_created"] += 1
            next_order += 1
        db.add(Activity(
            company_id=company_id, contact_id=contact_id,
            activity_type="sequence_resumed",
            content=f"[Admin] Re-anchored onto template '{t.name}' — {report['steps_created']} steps re-scheduled.",
        ))
        report["contacts_updated"] += 1

    await db.commit()
    return report
