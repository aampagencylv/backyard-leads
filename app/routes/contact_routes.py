"""
Contact-level routes: CRUD on Contacts and per-contact sequence generation.
"""
from __future__ import annotations
import json
import secrets
from typing import Optional
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db
from app.models import User, Company, Contact, GeneratedEmail, Activity
from app.auth import get_current_user
from app.services.email_generator import generate_cold_email, generate_follow_up

router = APIRouter(prefix="/api", tags=["contacts"])


# ============================================================
# Global list of all contacts (for the Contacts page)
# ============================================================

@router.get("/contacts")
async def list_all_contacts(
    company_status: Optional[str] = None,
    has_email: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List every contact across all companies, with company name attached."""
    query = (
        select(Contact, Company.name, Company.status)
        .join(Company, Contact.company_id == Company.id)
        .order_by(Contact.updated_at.desc())
    )
    if company_status:
        query = query.where(Company.status == company_status)
    if has_email is True:
        query = query.where(Contact.email.isnot(None), Contact.email != "")
    elif has_email is False:
        query = query.where((Contact.email.is_(None)) | (Contact.email == ""))

    rows = (await db.execute(query)).all()
    return [
        {
            "id": c.id,
            "company_id": c.company_id,
            "company_name": cname,
            "company_status": cstatus,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "name": c.full_name,
            "title": c.title,
            "email": c.email,
            "phone": c.phone,
            "linkedin_url": c.linkedin_url,
            "is_primary": c.is_primary,
            "email_status": c.email_status,
            "unsubscribed_at": c.unsubscribed_at.isoformat() if c.unsubscribed_at else None,
        }
        for c, cname, cstatus in rows
    ]


# ============================================================
# CRUD
# ============================================================

class CreateContactRequest(BaseModel):
    first_name: str = ""
    last_name: str = ""
    title: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    linkedin_url: Optional[str] = None
    is_primary: bool = False


@router.get("/companies/{company_id}/contacts")
async def list_contacts(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Contact)
        .where(Contact.company_id == company_id)
        .order_by(Contact.is_primary.desc(), Contact.id)
    )
    return [_contact_summary(c) for c in result.scalars().all()]


@router.post("/companies/{company_id}/contacts")
async def create_contact(
    company_id: int,
    req: CreateContactRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    has_primary = (await db.execute(
        select(Contact).where(Contact.company_id == company_id, Contact.is_primary == True)
    )).scalar_one_or_none()

    if req.is_primary and has_primary:
        # Demote existing primary
        has_primary.is_primary = False

    contact = Contact(
        company_id=company_id,
        first_name=req.first_name.strip(),
        last_name=req.last_name.strip(),
        title=req.title,
        email=req.email,
        phone=req.phone,
        linkedin_url=req.linkedin_url,
        is_primary=req.is_primary or (has_primary is None),
        unsubscribe_token=secrets.token_urlsafe(24),
    )
    db.add(contact)
    db.add(Activity(company_id=company_id, user_id=user.id, activity_type="contact_added",
                    content=f"Contact added: {(req.first_name + ' ' + req.last_name).strip() or req.email or '(no name)'}"))
    await db.commit()
    await db.refresh(contact)
    return _contact_summary(contact)


class UpdateContactRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    title: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    linkedin_url: Optional[str] = None
    is_primary: Optional[bool] = None


@router.get("/contacts/{contact_id}")
async def get_contact(
    contact_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    return _contact_summary(contact)


@router.patch("/contacts/{contact_id}")
async def update_contact(
    contact_id: int,
    req: UpdateContactRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    for f in ("first_name", "last_name", "title", "email", "phone", "linkedin_url"):
        v = getattr(req, f)
        if v is not None:
            setattr(contact, f, v)

    if req.is_primary is True and not contact.is_primary:
        # Demote any other primary at the same company
        await db.execute(
            select(Contact).where(Contact.company_id == contact.company_id, Contact.is_primary == True)
        )
        prev_primary = (await db.execute(
            select(Contact).where(Contact.company_id == contact.company_id, Contact.is_primary == True)
        )).scalar_one_or_none()
        if prev_primary:
            prev_primary.is_primary = False
        contact.is_primary = True

    await db.commit()
    await db.refresh(contact)
    return _contact_summary(contact)


@router.delete("/contacts/{contact_id}")
async def delete_contact(
    contact_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    company_id = contact.company_id
    await db.delete(contact)
    db.add(Activity(company_id=company_id, user_id=user.id, activity_type="contact_removed",
                    content=f"Contact removed: {contact.full_name or contact.email or '(no name)'}"))
    await db.commit()
    return {"deleted": True}


# ============================================================
# Per-contact sequence generation
# ============================================================

CONTACT_SEQUENCE_SCHEDULE = [
    {"order": 1, "type": "cold",        "delay_days": 0},
    {"order": 2, "type": "follow_up_1", "delay_days": 3},
    {"order": 3, "type": "follow_up_2", "delay_days": 7},
    {"order": 4, "type": "breakup",     "delay_days": 14},
]


@router.post("/contacts/{contact_id}/generate-sequence")
async def generate_contact_sequence(
    contact_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate a 4-email sequence for THIS contact (e.g. a second decision-maker at the same company)."""
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    company = (await db.execute(select(Company).where(Company.id == contact.company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if not company.enriched or not company.problems_found:
        raise HTTPException(status_code=400, detail="Company must be enriched before generating a sequence.")

    problems = json.loads(company.problems_found) if company.problems_found else []
    if not problems:
        raise HTTPException(status_code=400, detail="No problems found to reference in sequence.")

    # Skip if this contact already has emails
    existing = (await db.execute(
        select(GeneratedEmail).where(GeneratedEmail.contact_id == contact_id)
    )).first()
    if existing:
        raise HTTPException(status_code=400, detail="This contact already has a sequence. Delete it first to regenerate.")

    now = datetime.now(timezone.utc)
    first_subject = None
    created = 0

    for step in CONTACT_SEQUENCE_SCHEDULE:
        try:
            if step["order"] == 1:
                email_data = await generate_cold_email(
                    business_name=company.name,
                    business_type=company.business_type or "home services",
                    website=company.website or "",
                    problems=problems,
                    contact_name=contact.full_name or None,
                    location=f"{company.city}, {company.state}" if company.city else None,
                )
                first_subject = email_data["subject"]
            else:
                email_data = await generate_follow_up(
                    business_name=company.name,
                    business_type=company.business_type or "home services",
                    problems=problems,
                    previous_email_subject=first_subject or company.name,
                    follow_up_number=step["order"] - 1,
                    contact_name=contact.full_name or None,
                )

            email = GeneratedEmail(
                contact_id=contact.id,
                company_id=company.id,
                subject=email_data["subject"],
                body=email_data["body"],
                email_type=step["type"],
                sequence_order=step["order"],
                send_delay_days=step["delay_days"],
                scheduled_send_at=now + timedelta(days=step["delay_days"]),
                problems_referenced=json.dumps(problems[:2]),
            )
            db.add(email)
            await db.flush()
            created += 1
        except Exception:
            continue

    db.add(Activity(company_id=company.id, contact_id=contact.id, user_id=user.id,
                    activity_type="sequence_created",
                    content=f"Sequence created for {contact.full_name or contact.email or 'contact'} ({created} emails)"))
    company.email_generated = True
    if company.status == "new":
        company.status = "sequencing"
    await db.commit()

    return {"contact_id": contact.id, "emails_created": created}


# ============================================================
# Helpers
# ============================================================

def _contact_summary(c: Contact) -> dict:
    return {
        "id": c.id,
        "company_id": c.company_id,
        "first_name": c.first_name,
        "last_name": c.last_name,
        "name": c.full_name,
        "title": c.title,
        "email": c.email,
        "phone": c.phone,
        "linkedin_url": c.linkedin_url,
        "is_primary": c.is_primary,
        "email_status": c.email_status,
        "unsubscribed_at": c.unsubscribed_at.isoformat() if c.unsubscribed_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }
