"""
Feedback + Pending Deletion routes.

Feedback: any user can submit; admins can list + resolve.
Pending Deletions: BDR/BDR+ deletions land here; admins approve or reject.
"""
from __future__ import annotations
from typing import Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel

from app.database import get_db
from app.models import (
    User, Feedback, PendingDeletion,
    Company, Contact, Deal,
)
from app.auth import get_current_user, require_admin

router = APIRouter(prefix="/api", tags=["feedback"])


# ============================================================
# Feedback
# ============================================================

class SubmitFeedbackRequest(BaseModel):
    category: str = "feedback"  # feedback, bug, feature
    message: str
    page: Optional[str] = None


@router.post("/feedback")
async def submit_feedback(
    req: SubmitFeedbackRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message is required")
    fb = Feedback(
        user_id=user.id,
        category=req.category if req.category in ("feedback", "bug", "feature") else "feedback",
        message=req.message.strip()[:2000],
        page=req.page,
    )
    db.add(fb)
    await db.commit()
    return {"id": fb.id, "submitted": True}


@router.get("/feedback")
async def list_feedback(
    resolved: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    q = select(Feedback, User.first_name, User.last_name, User.email).join(User, Feedback.user_id == User.id)
    if resolved is not None:
        q = q.where(Feedback.resolved == resolved)
    q = q.order_by(Feedback.created_at.desc()).limit(100)
    rows = (await db.execute(q)).all()
    return [
        {
            "id": f.id,
            "category": f.category,
            "message": f.message,
            "page": f.page,
            "resolved": f.resolved,
            "admin_notes": f.admin_notes,
            "user_name": f"{first} {last}".strip(),
            "user_email": email,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        }
        for f, first, last, email in rows
    ]


@router.patch("/feedback/{feedback_id}")
async def update_feedback(
    feedback_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
    resolved: Optional[bool] = None,
    admin_notes: Optional[str] = None,
):
    fb = (await db.execute(select(Feedback).where(Feedback.id == feedback_id))).scalar_one_or_none()
    if not fb:
        raise HTTPException(status_code=404)
    if resolved is not None:
        fb.resolved = resolved
    if admin_notes is not None:
        fb.admin_notes = admin_notes.strip()[:500]
    await db.commit()
    return {"ok": True}


# ============================================================
# Pending Deletions
# ============================================================

class RequestDeletionBody(BaseModel):
    entity_type: str  # company, contact, deal
    entity_id: int
    reason: Optional[str] = None


@router.post("/deletions/request")
async def request_deletion(
    req: RequestDeletionBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """BDR/BDR+ request a deletion — goes to pending queue for admin approval."""
    if req.entity_type not in ("company", "contact", "deal"):
        raise HTTPException(status_code=400, detail="entity_type must be company, contact, or deal")

    # Get the entity name for display
    entity_name = None
    if req.entity_type == "company":
        obj = (await db.execute(select(Company).where(Company.id == req.entity_id))).scalar_one_or_none()
        entity_name = obj.name if obj else None
    elif req.entity_type == "contact":
        obj = (await db.execute(select(Contact).where(Contact.id == req.entity_id))).scalar_one_or_none()
        entity_name = obj.full_name if obj else None
    elif req.entity_type == "deal":
        obj = (await db.execute(select(Deal).where(Deal.id == req.entity_id))).scalar_one_or_none()
        entity_name = obj.name if obj else None

    if not obj:
        raise HTTPException(status_code=404, detail="Entity not found")

    # Check for existing pending request
    existing = (await db.execute(
        select(PendingDeletion).where(
            PendingDeletion.entity_type == req.entity_type,
            PendingDeletion.entity_id == req.entity_id,
            PendingDeletion.status == "pending",
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="A deletion request is already pending for this item")

    pd = PendingDeletion(
        requested_by=user.id,
        entity_type=req.entity_type,
        entity_id=req.entity_id,
        entity_name=entity_name,
        reason=(req.reason or "").strip()[:255] or None,
    )
    db.add(pd)
    await db.commit()
    return {"id": pd.id, "status": "pending", "entity_name": entity_name}


@router.get("/deletions/pending")
async def list_pending_deletions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    rows = (await db.execute(
        select(PendingDeletion, User.first_name, User.last_name)
        .join(User, PendingDeletion.requested_by == User.id)
        .where(PendingDeletion.status == "pending")
        .order_by(PendingDeletion.created_at.desc())
    )).all()
    return [
        {
            "id": pd.id,
            "entity_type": pd.entity_type,
            "entity_id": pd.entity_id,
            "entity_name": pd.entity_name,
            "reason": pd.reason,
            "requested_by_name": f"{first} {last}".strip(),
            "created_at": pd.created_at.isoformat() if pd.created_at else None,
        }
        for pd, first, last in rows
    ]


@router.get("/deletions/pending/count")
async def pending_deletion_count(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role not in ("admin", "super_admin"):
        return {"count": 0}
    count = (await db.execute(
        select(func.count(PendingDeletion.id)).where(PendingDeletion.status == "pending")
    )).scalar() or 0
    return {"count": count}


@router.post("/deletions/{deletion_id}/approve")
async def approve_deletion(
    deletion_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    pd = (await db.execute(select(PendingDeletion).where(PendingDeletion.id == deletion_id))).scalar_one_or_none()
    if not pd or pd.status != "pending":
        raise HTTPException(status_code=404)

    # Actually delete the entity
    if pd.entity_type == "company":
        obj = (await db.execute(select(Company).where(Company.id == pd.entity_id))).scalar_one_or_none()
        if obj:
            await db.delete(obj)
    elif pd.entity_type == "contact":
        obj = (await db.execute(select(Contact).where(Contact.id == pd.entity_id))).scalar_one_or_none()
        if obj:
            await db.delete(obj)
    elif pd.entity_type == "deal":
        obj = (await db.execute(select(Deal).where(Deal.id == pd.entity_id))).scalar_one_or_none()
        if obj:
            await db.delete(obj)

    pd.status = "approved"
    pd.reviewed_by = user.id
    pd.reviewed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"approved": True, "entity_type": pd.entity_type, "entity_name": pd.entity_name}


@router.post("/deletions/{deletion_id}/reject")
async def reject_deletion(
    deletion_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    pd = (await db.execute(select(PendingDeletion).where(PendingDeletion.id == deletion_id))).scalar_one_or_none()
    if not pd or pd.status != "pending":
        raise HTTPException(status_code=404)
    pd.status = "rejected"
    pd.reviewed_by = user.id
    pd.reviewed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"rejected": True, "entity_name": pd.entity_name}
