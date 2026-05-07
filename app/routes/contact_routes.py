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
from sqlalchemy import select, func
from pydantic import BaseModel

from app.database import get_db
from app.models import User, Company, Contact, GeneratedEmail, Activity
from app.auth import get_current_user
from app.services.email_generator import generate_cold_email, generate_follow_up
from app.services.netrows_enrichment import (
    reverse_email_lookup as netrows_reverse_lookup,
    linkedin_recent_posts as netrows_linkedin_posts,
    find_email_by_name as netrows_find_email_by_name,
    find_email_by_linkedin as netrows_find_email_by_linkedin,
)
from app.services.hunter_enrichment import verify_email as hunter_verify_email
from app.config import settings
from app.runtime_config import get_netrows_api_key

router = APIRouter(prefix="/api", tags=["contacts"])


# ============================================================
# Global list of all contacts (for the Contacts page)
# ============================================================

@router.get("/contacts")
async def list_all_contacts(
    company_status: Optional[str] = None,
    has_email: Optional[bool] = None,
    has_phone: Optional[bool] = None,  # NEW: power-dialer filter
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List every contact across all companies, with company name and sequence status."""
    email_count_sq = (
        select(
            GeneratedEmail.contact_id,
            func.count(GeneratedEmail.id).label("email_count"),
        )
        .group_by(GeneratedEmail.contact_id)
        .subquery()
    )
    query = (
        select(Contact, Company.name, Company.status, Company.phone.label("company_phone"),
               func.coalesce(email_count_sq.c.email_count, 0).label("email_count"))
        .join(Company, Contact.company_id == Company.id)
        .outerjoin(email_count_sq, Contact.id == email_count_sq.c.contact_id)
        .order_by(Contact.updated_at.desc())
    )
    if company_status:
        query = query.where(Company.status == company_status)
    if has_email is True:
        query = query.where(Contact.email.isnot(None), Contact.email != "")
    elif has_email is False:
        query = query.where((Contact.email.is_(None)) | (Contact.email == ""))

    if has_phone is True:
        query = query.where(Contact.phone.isnot(None), Contact.phone != "")
    elif has_phone is False:
        query = query.where((Contact.phone.is_(None)) | (Contact.phone == ""))

    rows = (await db.execute(query)).all()
    return [
        {
            "id": c.id,
            "company_id": c.company_id,
            "company_name": cname,
            "company_status": cstatus,
            "company_phone": cphone,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "name": c.full_name,
            "title": c.title,
            "email": c.email,
            "phone": c.phone,
            "linkedin_url": c.linkedin_url,
            "is_primary": c.is_primary,
            "email_status": c.email_status,
            "has_sequence": ecount > 0,
            "unsubscribed_at": c.unsubscribed_at.isoformat() if c.unsubscribed_at else None,
            "do_not_text": bool(c.do_not_text),
            "do_not_text_at": c.do_not_text_at.isoformat() if c.do_not_text_at else None,
        }
        for c, cname, cstatus, cphone, ecount in rows
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

    # If we have an email but no name, auto-fire Netrows reverse-lookup (1 credit)
    first = req.first_name.strip()
    last = req.last_name.strip()
    title = req.title
    linkedin = req.linkedin_url
    if req.email and not first and not last and await get_netrows_api_key(db):
        try:
            looked_up = await netrows_reverse_lookup(req.email, await get_netrows_api_key(db))
            if looked_up:
                first = looked_up.first_name or first
                last = looked_up.last_name or last
                title = title or looked_up.current_title or looked_up.headline
                linkedin = linkedin or looked_up.linkedin_url
        except Exception:
            pass

    contact = Contact(
        company_id=company_id,
        first_name=first, last_name=last,
        title=title,
        email=req.email,
        phone=req.phone,
        linkedin_url=linkedin,
        is_primary=req.is_primary or (has_primary is None),
        unsubscribe_token=secrets.token_urlsafe(24),
    )
    db.add(contact)
    db.add(Activity(company_id=company_id, user_id=user.id, activity_type="contact_added",
                    content=f"Contact added: {(first + ' ' + last).strip() or req.email or '(no name)'}"))
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
            "do_not_text": bool(c.do_not_text),
            "do_not_text_at": c.do_not_text_at.isoformat() if c.do_not_text_at else None,
        "recent_posts": json.loads(c.recent_posts_json) if c.recent_posts_json else [],
        "posts_fetched_at": c.posts_fetched_at.isoformat() if c.posts_fetched_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


# ============================================================
# Netrows-powered contact actions: refresh posts, lookup email
# ============================================================

@router.post("/contacts/{contact_id}/refresh-posts")
async def refresh_contact_posts(
    contact_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Pull recent LinkedIn posts for personalization context (1 credit)."""
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if not contact.linkedin_url:
        raise HTTPException(status_code=400, detail="Contact has no LinkedIn URL on file")
    if not await get_netrows_api_key(db):
        raise HTTPException(status_code=400, detail="Netrows API key not configured")

    posts = await netrows_linkedin_posts(contact.linkedin_url, await get_netrows_api_key(db), limit=5)
    contact.recent_posts_json = json.dumps([{
        "text": p.text, "posted_at": p.posted_at, "url": p.url,
        "likes": p.likes, "comments": p.comments,
    } for p in posts])
    contact.posts_fetched_at = datetime.now(timezone.utc)
    await db.commit()
    return {"posts_count": len(posts), "fetched_at": contact.posts_fetched_at.isoformat()}


class LookupEmailRequest(BaseModel):
    domain: Optional[str] = None  # if not given, derived from contact's company website


@router.post("/contacts/{contact_id}/lookup-email")
async def lookup_contact_email(
    contact_id: int,
    req: LookupEmailRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Use Netrows to find a verified email for this contact.
    Tries by-linkedin first (5 cr), falls back to by-name (5 cr) if we have first+last.
    """
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if not await get_netrows_api_key(db):
        raise HTTPException(status_code=400, detail="Netrows API key not configured")

    # Resolve domain
    domain = req.domain
    if not domain:
        company = (await db.execute(select(Company).where(Company.id == contact.company_id))).scalar_one_or_none()
        if company and company.website:
            domain = company.website

    found = None
    if contact.linkedin_url:
        found = await netrows_find_email_by_linkedin(contact.linkedin_url, await get_netrows_api_key(db))
    if not found and contact.first_name and contact.last_name and domain:
        found = await netrows_find_email_by_name(contact.first_name, contact.last_name,
                                                  domain, await get_netrows_api_key(db))

    if not found or not found.email:
        raise HTTPException(status_code=404, detail="Could not find a verified email")

    contact.email = found.email
    contact.email_status = found.email_status
    if found.full_name and not contact.full_name:
        parts = found.full_name.strip().split(maxsplit=1)
        contact.first_name = contact.first_name or parts[0]
        if len(parts) > 1:
            contact.last_name = contact.last_name or parts[1]
    if found.linkedin_url and not contact.linkedin_url:
        contact.linkedin_url = found.linkedin_url
    db.add(Activity(company_id=contact.company_id, contact_id=contact.id, user_id=user.id,
                    activity_type="email_found",
                    content=f"Email found via Netrows: {found.email} ({found.email_status})"))
    await db.commit()
    await db.refresh(contact)
    return _contact_summary(contact)


# ============================================================
# Email verification via Hunter
# ============================================================

@router.post("/contacts/{contact_id}/verify-email")
async def verify_contact_email(
    contact_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Verify a contact's email via Hunter's email verifier."""
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if not contact.email:
        raise HTTPException(status_code=400, detail="Contact has no email to verify")
    if not settings.hunter_api_key:
        raise HTTPException(status_code=400, detail="Hunter API key not configured")

    result = await hunter_verify_email(contact.email, settings.hunter_api_key)
    hunter_result = result.get("result", "unknown")
    score = result.get("score", 0)

    if hunter_result == "deliverable":
        contact.email_status = "valid"
    elif hunter_result == "undeliverable":
        contact.email_status = "invalid"
    else:
        contact.email_status = "unknown"

    db.add(Activity(
        company_id=contact.company_id, contact_id=contact.id, user_id=user.id,
        activity_type="email_verified",
        content=f"Email verified: {contact.email} -> {hunter_result} (score: {score})",
    ))
    await db.commit()

    return {
        "contact_id": contact.id,
        "email": contact.email,
        "email_status": contact.email_status,
        "result": hunter_result,
        "score": score,
    }
