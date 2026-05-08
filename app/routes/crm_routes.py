"""
CRM cross-cutting routes: tags, tasks, activity timeline, search, users.
Operates on companies (not leads). Per-deal helpers live in deal_routes.
"""
from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from pydantic import BaseModel
from datetime import datetime, timezone

from app.database import get_db
from app.models import User, Company, Contact, Activity, Tag, Task, GeneratedEmail, company_tags
from app.auth import get_current_user
import json

router = APIRouter(prefix="/api/crm", tags=["crm"])


# ============================================================
# Activity timeline
# ============================================================

class AddNoteRequest(BaseModel):
    content: str
    activity_type: str = "note"  # note, call, meeting, linkedin_message
    contact_id: Optional[int] = None
    deal_id: Optional[int] = None


@router.get("/companies/{company_id}/timeline")
async def get_timeline(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Verify company ownership
    from app.scoping import check_company_access
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if not check_company_access(company, user):
        raise HTTPException(status_code=404, detail="Company not found")

    result = await db.execute(
        select(Activity).where(Activity.company_id == company_id).order_by(Activity.created_at.desc())
    )
    activities = result.scalars().all()
    user_ids = {a.user_id for a in activities if a.user_id}
    user_names: dict[int, str] = {}
    if user_ids:
        u_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        for u in u_result.scalars().all():
            user_names[u.id] = u.full_name

    return [
        {
            "id": a.id,
            "type": a.activity_type,
            "content": a.content,
            "user_name": user_names.get(a.user_id),
            "contact_id": a.contact_id,
            "deal_id": a.deal_id,
            "metadata": json.loads(a.metadata_json) if a.metadata_json else None,
            "reply_sentiment": a.reply_sentiment,
            "reply_sentiment_summary": a.reply_sentiment_summary,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in activities
    ]


@router.post("/companies/{company_id}/note")
async def add_note(
    company_id: int,
    req: AddNoteRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    activity = Activity(
        company_id=company_id,
        contact_id=req.contact_id,
        deal_id=req.deal_id,
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
    company_id: int,
    activity_type: str,
    content: str,
    user_id: Optional[int] = None,
    contact_id: Optional[int] = None,
    deal_id: Optional[int] = None,
    metadata: Optional[dict] = None,
):
    """Helper to log an activity from anywhere in the codebase."""
    db.add(Activity(
        company_id=company_id,
        contact_id=contact_id,
        deal_id=deal_id,
        user_id=user_id,
        activity_type=activity_type,
        content=content,
        metadata_json=json.dumps(metadata) if metadata else None,
    ))


# ============================================================
# Tags
# ============================================================

class CreateTagRequest(BaseModel):
    name: str
    color: str = "#1B5E20"


@router.get("/tags")
async def list_tags(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    result = await db.execute(select(Tag))
    return [{"id": t.id, "name": t.name, "color": t.color} for t in result.scalars().all()]


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


@router.post("/companies/{company_id}/tags/{tag_id}")
async def add_tag_to_company(
    company_id: int,
    tag_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    tag = (await db.execute(select(Tag).where(Tag.id == tag_id))).scalar_one_or_none()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    # Manual association (async SQLAlchemy can't lazy-load company.tags)
    existing = (await db.execute(
        select(company_tags).where(
            company_tags.c.company_id == company_id,
            company_tags.c.tag_id == tag_id,
        )
    )).first()
    if not existing:
        await db.execute(company_tags.insert().values(company_id=company_id, tag_id=tag_id))
        await db.commit()
    return {"company_id": company_id, "tag": tag.name}


@router.delete("/companies/{company_id}/tags/{tag_id}")
async def remove_tag_from_company(
    company_id: int,
    tag_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await db.execute(
        company_tags.delete().where(
            company_tags.c.company_id == company_id,
            company_tags.c.tag_id == tag_id,
        )
    )
    await db.commit()
    return {"removed": True}


# ============================================================
# Tasks
# ============================================================

class CreateTaskRequest(BaseModel):
    description: str
    due_date: Optional[str] = None
    contact_id: Optional[int] = None
    deal_id: Optional[int] = None


@router.get("/companies/{company_id}/tasks")
async def get_company_tasks(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Task).where(Task.company_id == company_id).order_by(Task.completed, Task.due_date)
    )
    return [
        {
            "id": t.id,
            "description": t.description,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "completed": t.completed,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            "contact_id": t.contact_id,
            "deal_id": t.deal_id,
        }
        for t in result.scalars().all()
    ]


@router.get("/tasks/upcoming")
async def get_upcoming_tasks(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """All open tasks for the current user, with company name attached."""
    result = await db.execute(
        select(Task, Company.name)
        .join(Company, Task.company_id == Company.id)
        .where(Task.user_id == user.id, Task.completed == False)
        .order_by(Task.due_date.is_(None), Task.due_date)
    )
    return [
        {
            "id": t.id,
            "description": t.description,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "company_id": t.company_id,
            "company_name": cname,
        }
        for t, cname in result.all()
    ]


@router.get("/tasks/all")
async def get_all_open_tasks(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """All open tasks across the team."""
    result = await db.execute(
        select(Task, Company.name, User.first_name, User.last_name)
        .join(Company, Task.company_id == Company.id)
        .join(User, Task.user_id == User.id)
        .where(Task.completed == False)
        .order_by(Task.due_date.is_(None), Task.due_date)
    )
    out = []
    for t, cname, ufirst, ulast in result.all():
        out.append({
            "id": t.id,
            "description": t.description,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "company_id": t.company_id,
            "company_name": cname,
            "owner_name": f"{ufirst} {ulast}".strip(),
        })
    return out


@router.post("/companies/{company_id}/tasks")
async def create_task(
    company_id: int,
    req: CreateTaskRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    due = None
    if req.due_date:
        try:
            due = datetime.fromisoformat(req.due_date.replace("Z", "+00:00"))
        except ValueError:
            pass

    task = Task(
        company_id=company_id,
        contact_id=req.contact_id,
        deal_id=req.deal_id,
        user_id=user.id,
        description=req.description,
        due_date=due,
    )
    db.add(task)
    await log_activity(db, company_id, "task_created", f"Task: {req.description}", user.id,
                       contact_id=req.contact_id, deal_id=req.deal_id)
    await db.commit()
    await db.refresh(task)
    return {"id": task.id, "description": task.description,
            "due_date": task.due_date.isoformat() if task.due_date else None}


@router.patch("/tasks/{task_id}/complete")
async def complete_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.completed = True
    task.completed_at = datetime.now(timezone.utc)
    await log_activity(db, task.company_id, "task_completed", f"Completed: {task.description}", user.id,
                       contact_id=task.contact_id, deal_id=task.deal_id)
    await db.commit()
    return {"id": task.id, "completed": True}


# ============================================================
# Search
# ============================================================

@router.get("/search")
async def search_crm(
    q: str = Query(..., min_length=2),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Search companies and contacts by name, email, city, or phone."""
    pattern = f"%{q}%"

    # Companies
    companies = (await db.execute(
        select(Company).where(
            or_(
                Company.name.ilike(pattern),
                Company.city.ilike(pattern),
                Company.phone.ilike(pattern),
            )
        ).order_by(Company.updated_at.desc()).limit(30)
    )).scalars().all()

    # Contacts (with their company)
    contact_rows = (await db.execute(
        select(Contact, Company.name)
        .join(Company, Contact.company_id == Company.id)
        .where(
            or_(
                Contact.first_name.ilike(pattern),
                Contact.last_name.ilike(pattern),
                Contact.email.ilike(pattern),
                Contact.phone.ilike(pattern),
            )
        ).order_by(Contact.updated_at.desc()).limit(30)
    )).all()

    return {
        "companies": [
            {
                "id": c.id, "name": c.name,
                "city": c.city, "state": c.state,
                "status": c.status,
            }
            for c in companies
        ],
        "contacts": [
            {
                "id": c.id, "name": c.full_name, "email": c.email,
                "company_id": c.company_id, "company_name": cname,
            }
            for c, cname in contact_rows
        ],
    }


# ============================================================
# Users (for assignment dropdowns)
# ============================================================

@router.get("/users")
async def list_users(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(User).where(User.is_active == True))
    return [{"id": u.id, "name": u.full_name, "email": u.email} for u in result.scalars().all()]
