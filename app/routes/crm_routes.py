"""
CRM routes — activities, notes, tasks, tags, deal management, lead detail.
"""
from __future__ import annotations
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, func
from pydantic import BaseModel
from datetime import datetime, timezone
from app.database import get_db
from app.models import User, Lead, Activity, Tag, Task, lead_tags, GeneratedEmail
from app.auth import get_current_user
import json

router = APIRouter(prefix="/api/crm", tags=["crm"])


# ============ Activity Timeline ============

class AddNoteRequest(BaseModel):
    content: str
    activity_type: str = "note"  # note, call, meeting, linkedin_message


@router.get("/leads/{lead_id}/timeline")
async def get_timeline(
    lead_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get full activity timeline for a lead — notes, calls, emails, status changes."""
    result = await db.execute(
        select(Activity)
        .where(Activity.lead_id == lead_id)
        .order_by(Activity.created_at.desc())
    )
    activities = result.scalars().all()

    timeline = []
    for a in activities:
        # Get user name
        user_name = None
        if a.user_id:
            u_result = await db.execute(select(User).where(User.id == a.user_id))
            u = u_result.scalar_one_or_none()
            user_name = u.full_name if u else None

        timeline.append({
            "id": a.id,
            "type": a.activity_type,
            "content": a.content,
            "user_name": user_name,
            "metadata": json.loads(a.metadata_json) if a.metadata_json else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })

    return timeline


@router.post("/leads/{lead_id}/note")
async def add_note(
    lead_id: int,
    req: AddNoteRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Add a note, call log, or meeting note to a lead's timeline."""
    # Verify lead exists
    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    activity = Activity(
        lead_id=lead_id,
        user_id=user.id,
        activity_type=req.activity_type,
        content=req.content,
    )
    db.add(activity)
    await db.commit()
    await db.refresh(activity)

    return {
        "id": activity.id,
        "type": activity.activity_type,
        "content": activity.content,
        "user_name": user.full_name,
        "created_at": activity.created_at.isoformat(),
    }


async def log_activity(
    db: AsyncSession,
    lead_id: int,
    activity_type: str,
    content: str,
    user_id: int = None,
    metadata: dict = None,
):
    """Helper to log an activity from anywhere in the codebase."""
    activity = Activity(
        lead_id=lead_id,
        user_id=user_id,
        activity_type=activity_type,
        content=content,
        metadata_json=json.dumps(metadata) if metadata else None,
    )
    db.add(activity)


# ============ Tags ============

class CreateTagRequest(BaseModel):
    name: str
    color: str = "#1B5E20"


@router.get("/tags")
async def list_tags(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Tag))
    tags = result.scalars().all()
    return [{"id": t.id, "name": t.name, "color": t.color} for t in tags]


@router.post("/tags")
async def create_tag(
    req: CreateTagRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tag = Tag(name=req.name, color=req.color)
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return {"id": tag.id, "name": tag.name, "color": tag.color}


@router.post("/leads/{lead_id}/tags/{tag_id}")
async def add_tag_to_lead(
    lead_id: int,
    tag_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    tag_result = await db.execute(select(Tag).where(Tag.id == tag_id))
    tag = tag_result.scalar_one_or_none()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")

    if tag not in lead.tags:
        lead.tags.append(tag)
        await db.commit()

    return {"lead_id": lead_id, "tag": tag.name}


@router.delete("/leads/{lead_id}/tags/{tag_id}")
async def remove_tag_from_lead(
    lead_id: int,
    tag_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    tag_result = await db.execute(select(Tag).where(Tag.id == tag_id))
    tag = tag_result.scalar_one_or_none()
    if tag and tag in lead.tags:
        lead.tags.remove(tag)
        await db.commit()

    return {"removed": True}


# ============ Tasks ============

class CreateTaskRequest(BaseModel):
    description: str
    due_date: Optional[str] = None  # ISO format


@router.get("/leads/{lead_id}/tasks")
async def get_lead_tasks(
    lead_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Task).where(Task.lead_id == lead_id).order_by(Task.completed, Task.due_date)
    )
    tasks = result.scalars().all()
    return [
        {
            "id": t.id,
            "description": t.description,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "completed": t.completed,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        }
        for t in tasks
    ]


@router.get("/tasks/upcoming")
async def get_upcoming_tasks(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get all incomplete tasks for the current user."""
    result = await db.execute(
        select(Task, Lead.business_name)
        .join(Lead, Task.lead_id == Lead.id)
        .where(Task.user_id == user.id, Task.completed == False)
        .order_by(Task.due_date)
    )
    rows = result.all()
    return [
        {
            "id": t.id,
            "description": t.description,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "lead_id": t.lead_id,
            "business_name": bname,
        }
        for t, bname in rows
    ]


@router.post("/leads/{lead_id}/tasks")
async def create_task(
    lead_id: int,
    req: CreateTaskRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    due = None
    if req.due_date:
        try:
            due = datetime.fromisoformat(req.due_date)
        except ValueError:
            pass

    task = Task(
        lead_id=lead_id,
        user_id=user.id,
        description=req.description,
        due_date=due,
    )
    db.add(task)
    await log_activity(db, lead_id, "task_created", f"Task created: {req.description}", user.id)
    await db.commit()
    await db.refresh(task)

    return {"id": task.id, "description": task.description, "due_date": task.due_date}


@router.patch("/tasks/{task_id}/complete")
async def complete_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task.completed = True
    task.completed_at = datetime.now(timezone.utc)
    await log_activity(db, task.lead_id, "task_completed", f"Completed: {task.description}", user.id)
    await db.commit()

    return {"id": task.id, "completed": True}


# ============ Deal Management ============

class UpdateDealRequest(BaseModel):
    deal_value: Optional[float] = None
    deal_stage: Optional[str] = None
    assigned_to: Optional[int] = None


@router.patch("/leads/{lead_id}/deal")
async def update_deal(
    lead_id: int,
    req: UpdateDealRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    changes = []
    if req.deal_value is not None:
        lead.deal_value = req.deal_value
        changes.append(f"Deal value set to ${req.deal_value:,.0f}/mo")
    if req.deal_stage is not None:
        old = lead.deal_stage
        lead.deal_stage = req.deal_stage
        changes.append(f"Stage changed: {old} → {req.deal_stage}")
    if req.assigned_to is not None:
        lead.assigned_to = req.assigned_to
        u_result = await db.execute(select(User).where(User.id == req.assigned_to))
        assigned = u_result.scalar_one_or_none()
        changes.append(f"Assigned to {assigned.name if assigned else 'unknown'}")

    if changes:
        await log_activity(db, lead_id, "deal_update", "; ".join(changes), user.id)

    await db.commit()
    return {"lead_id": lead.id, "deal_value": lead.deal_value, "deal_stage": lead.deal_stage}


# ============ Lead Contact Update ============

class UpdateContactRequest(BaseModel):
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_title: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_linkedin: Optional[str] = None
    linkedin_url: Optional[str] = None


@router.patch("/leads/{lead_id}/contact")
async def update_contact(
    lead_id: int,
    req: UpdateContactRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    if req.contact_name is not None:
        lead.contact_name = req.contact_name
    if req.contact_email is not None:
        lead.contact_email = req.contact_email
    if req.contact_title is not None:
        lead.contact_title = req.contact_title
    if req.contact_phone is not None:
        lead.contact_phone = req.contact_phone
    if req.contact_linkedin is not None:
        lead.contact_linkedin = req.contact_linkedin
    if req.linkedin_url is not None:
        lead.linkedin_url = req.linkedin_url

    await log_activity(db, lead_id, "contact_updated", "Contact info updated", user.id)
    await db.commit()

    return {"lead_id": lead.id, "contact_name": lead.contact_name, "contact_email": lead.contact_email}


# ============ CRM Lead Detail (full view) ============

@router.get("/leads/{lead_id}/full")
async def get_lead_full(
    lead_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get complete lead record with timeline, tasks, tags, emails."""
    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    # Emails
    email_result = await db.execute(
        select(GeneratedEmail).where(GeneratedEmail.lead_id == lead_id).order_by(GeneratedEmail.sequence_order)
    )
    emails = email_result.scalars().all()

    # Timeline
    activity_result = await db.execute(
        select(Activity).where(Activity.lead_id == lead_id).order_by(Activity.created_at.desc())
    )
    activities = activity_result.scalars().all()

    # Tasks
    task_result = await db.execute(
        select(Task).where(Task.lead_id == lead_id).order_by(Task.completed, Task.due_date)
    )
    tasks = task_result.scalars().all()

    # Tags
    tag_list = [{"id": t.id, "name": t.name, "color": t.color} for t in lead.tags]

    # Assigned user
    assigned_name = None
    if lead.assigned_to:
        u_result = await db.execute(select(User).where(User.id == lead.assigned_to))
        u = u_result.scalar_one_or_none()
        assigned_name = u.full_name if u else None

    # Build user name lookup for activities
    user_ids = set(a.user_id for a in activities if a.user_id)
    user_names = {}
    if user_ids:
        u_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        for u in u_result.scalars().all():
            user_names[u.id] = u.full_name

    problems = json.loads(lead.problems_found) if lead.problems_found else []

    return {
        "id": lead.id,
        "business_name": lead.business_name,
        "phone": lead.phone,
        "website": lead.website,
        "address": lead.address,
        "city": lead.city,
        "state": lead.state,
        "rating": lead.rating,
        "review_count": lead.review_count,
        "business_type": lead.business_type,
        "status": lead.status,
        "enriched": lead.enriched,
        "enrichment_summary": lead.enrichment_summary,
        "problems_found": problems,
        "problem_count": len(problems),
        "tech_stack": json.loads(lead.tech_stack) if lead.tech_stack else [],
        "contact_name": lead.contact_name,
        "contact_email": lead.contact_email,
        "contact_title": lead.contact_title,
        "contact_phone": lead.contact_phone,
        "contact_linkedin": lead.contact_linkedin,
        "linkedin_url": lead.linkedin_url,
        "deal_value": lead.deal_value,
        "deal_stage": lead.deal_stage,
        "assigned_to": lead.assigned_to,
        "assigned_name": assigned_name,
        "tags": tag_list,
        "emails": [
            {
                "id": e.id,
                "subject": e.subject,
                "body": e.body,
                "email_type": e.email_type,
                "sequence_order": e.sequence_order,
                "send_delay_days": e.send_delay_days,
                "is_sent": e.is_sent,
                "sent_at": e.sent_at.isoformat() if e.sent_at else None,
            }
            for e in emails
        ],
        "timeline": [
            {
                "id": a.id,
                "type": a.activity_type,
                "content": a.content,
                "user_name": user_names.get(a.user_id),
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in activities
        ],
        "tasks": [
            {
                "id": t.id,
                "description": t.description,
                "due_date": t.due_date.isoformat() if t.due_date else None,
                "completed": t.completed,
            }
            for t in tasks
        ],
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
    }


# ============ Search across CRM ============

@router.get("/search")
async def search_crm(
    q: str = Query(..., min_length=2),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Search leads by business name, contact name, email, city, or phone."""
    pattern = f"%{q}%"
    result = await db.execute(
        select(Lead)
        .where(
            or_(
                Lead.business_name.ilike(pattern),
                Lead.contact_name.ilike(pattern),
                Lead.contact_email.ilike(pattern),
                Lead.city.ilike(pattern),
                Lead.phone.ilike(pattern),
            )
        )
        .order_by(Lead.updated_at.desc())
        .limit(50)
    )
    leads = result.scalars().all()

    return [
        {
            "id": l.id,
            "business_name": l.business_name,
            "contact_name": l.contact_name,
            "contact_email": l.contact_email,
            "city": l.city,
            "state": l.state,
            "status": l.status,
            "deal_stage": l.deal_stage,
        }
        for l in leads
    ]


# ============ Users list (for assignment) ============

@router.get("/users")
async def list_users(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(User).where(User.is_active == True))
    users = result.scalars().all()
    return [{"id": u.id, "name": u.full_name, "email": u.email} for u in users]
