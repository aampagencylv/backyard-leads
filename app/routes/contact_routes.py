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
    has_phone: Optional[bool] = None,
    rep_id: Optional[int] = None,  # Admin filter: show only this rep's contacts
    search: Optional[str] = None,  # Search by name or email
    email_status: Optional[str] = None,  # valid, invalid, bounced, unknown
    has_sequence: Optional[bool] = None,
    phone_type: Optional[str] = None,    # mobile, landline, voip, unknown
    opted_out: Optional[bool] = None,    # unsubscribed_at OR do_not_text
    hot_lead_recent: Optional[bool] = None,  # had any hot_lead Activity in last 30 min
    city: Optional[str] = None,          # ILIKE substring on Company.city
    state: Optional[str] = None,         # exact (or ILIKE) match on Company.state
    tag_id: Optional[int] = None,        # any tag on the contact's company
    sort_by: str = "updated",            # updated | name | company | created | email_status
    sort_dir: str = "desc",              # asc | desc
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List contacts with multi-tenant scoping and advanced filters/sorts."""
    from app.scoping import scope_contacts
    from app.models import Activity, company_tags
    from sqlalchemy import or_, exists

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
    )

    # Multi-tenant scoping
    query = scope_contacts(query, user, rep_id)

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

    if search:
        pattern = f"%{search}%"
        query = query.where(or_(
            (Contact.first_name + " " + Contact.last_name).ilike(pattern),
            Contact.email.ilike(pattern),
            Company.name.ilike(pattern),
        ))

    if email_status:
        query = query.where(Contact.email_status == email_status)

    if has_sequence is True:
        query = query.where(email_count_sq.c.email_count > 0)
    elif has_sequence is False:
        query = query.where(func.coalesce(email_count_sq.c.email_count, 0) == 0)

    if phone_type:
        # Treat 'unknown' as "either NULL or literally 'unknown' or 'error'"
        if phone_type == "unknown":
            query = query.where(or_(Contact.phone_type.is_(None), Contact.phone_type.in_(("unknown", "error"))))
        else:
            query = query.where(Contact.phone_type == phone_type)

    if opted_out is True:
        query = query.where(or_(Contact.unsubscribed_at.isnot(None), Contact.do_not_text == True))
    elif opted_out is False:
        query = query.where(Contact.unsubscribed_at.is_(None), Contact.do_not_text == False)

    if hot_lead_recent is True:
        # EXISTS subquery: a hot_lead Activity within the last 30 min
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        query = query.where(exists(
            select(Activity.id).where(
                Activity.contact_id == Contact.id,
                Activity.activity_type == "hot_lead",
                Activity.created_at >= cutoff,
            )
        ))

    if city:
        query = query.where(Company.city.ilike(f"%{city}%"))
    if state:
        query = query.where(Company.state.ilike(state))

    if tag_id:
        # Company has this tag (via the company_tags association)
        query = query.where(exists(
            select(company_tags.c.company_id).where(
                company_tags.c.company_id == Company.id,
                company_tags.c.tag_id == tag_id,
            )
        ))

    # Sort — sane defaults, configurable by query param
    sort_dir_lower = (sort_dir or "desc").lower()
    desc_first = sort_dir_lower == "desc"
    sort_col_map = {
        "updated":      Contact.updated_at,
        "created":      Contact.created_at,
        "name":         Contact.last_name,  # primary sort; first_name as tiebreak below
        "company":      Company.name,
        "email_status": Contact.email_status,
    }
    sort_col = sort_col_map.get(sort_by, Contact.updated_at)
    primary = sort_col.desc() if desc_first else sort_col.asc()
    if sort_by == "name":
        # Tiebreak by first_name in the same direction
        secondary = Contact.first_name.desc() if desc_first else Contact.first_name.asc()
        query = query.order_by(primary, secondary)
    else:
        query = query.order_by(primary)

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
            "phone_type": c.phone_type,
            "phone_carrier": c.phone_carrier,
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

    # Eager Twilio Lookup on phone — populates phone_type so the contact
    # card shows the badge immediately and SMS/voice gating works on first
    # send attempt instead of paying the latency on the hot path. ~$0.005.
    if req.phone:
        try:
            from app.services.twilio_voice import lookup_phone_type
            from app.runtime_config import get_twilio_credentials
            creds = await get_twilio_credentials(db)
            if creds and creds.is_minimally_configured:
                r = await lookup_phone_type(creds, req.phone)
                if r.type and r.type not in ("error",):
                    contact.phone_type = r.type
                    contact.phone_carrier = r.carrier
                    contact.phone_type_checked_at = datetime.now(timezone.utc)
                    await db.commit()
        except Exception:
            pass  # Lookup failure must not block contact create

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
    from app.scoping import check_contact_access
    if not await check_contact_access(contact, user, db):
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
    from app.scoping import check_contact_access
    if not await check_contact_access(contact, user, db):
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
    from app.scoping import check_contact_access
    if not await check_contact_access(contact, user, db):
        raise HTTPException(status_code=404, detail="Contact not found")
    company_id = contact.company_id
    await db.delete(contact)
    db.add(Activity(company_id=company_id, user_id=user.id, activity_type="contact_removed",
                    content=f"Contact removed: {contact.full_name or contact.email or '(no name)'}"))
    await db.commit()
    return {"deleted": True}


class BatchContactAction(BaseModel):
    contact_ids: list
    action: str  # delete


@router.post("/contacts/batch")
async def batch_contact_action(
    req: BatchContactAction,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Batch actions on contacts: delete."""
    from app.scoping import check_contact_access
    count = 0
    if req.action == "delete":
        for cid in req.contact_ids:
            contact = (await db.execute(select(Contact).where(Contact.id == cid))).scalar_one_or_none()
            if contact and await check_contact_access(contact, user, db):
                await db.delete(contact)
                count += 1
        await db.commit()
    return {"action": req.action, "count": count}


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

    # Get-or-create the AI Findability audit so follow-up emails can
    # share the link. ensure_audit_for_company returns None if anything
    # fails — the sequence still generates, just without the link.
    from app.services.audit_report import ensure_audit_for_company
    audit_url = await ensure_audit_for_company(db, company)

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
                    audit_url=audit_url,
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

    try:
        from app.services.webhook_dispatch import dispatch_event
        await dispatch_event(db, "sequence.created", {
            "contact_id": contact.id,
            "company_id": company.id,
            "company_name": company.name,
            "contact_email": contact.email,
            "step_count": created,
            "kind": "contact_outreach",
        })
    except Exception:
        pass

    # Auto-add company to pipeline if no deal exists
    from app.models import Deal
    existing_deal = (await db.execute(
        select(Deal).where(Deal.company_id == company.id)
    )).scalar_one_or_none()
    if not existing_deal:
        from app.routes.deal_routes import recommend_package
        pkg = recommend_package(company.employee_count)
        deal = Deal(
            company_id=company.id,
            name=f"{company.name} — Initial Deal",
            value=0,
            package=pkg,
            contract_months=6,
            stage="in_sequence",
            probability=0,
            assigned_to=company.assigned_to or user.id,
        )
        db.add(deal)

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
        "phone_type": c.phone_type,
        "phone_carrier": c.phone_carrier,
        "recent_posts": json.loads(c.recent_posts_json) if c.recent_posts_json else [],
        "posts_fetched_at": c.posts_fetched_at.isoformat() if c.posts_fetched_at else None,
        "custom_fields": json.loads(c.custom_fields_json) if c.custom_fields_json else {},
        "linkedin_profile": json.loads(c.linkedin_profile_json) if c.linkedin_profile_json else None,
        "linkedin_profile_fetched_at": c.linkedin_profile_fetched_at.isoformat() if c.linkedin_profile_fetched_at else None,
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
        raise HTTPException(status_code=400, detail="Data enrichment not configured")

    posts = await netrows_linkedin_posts(contact.linkedin_url, await get_netrows_api_key(db), limit=5)
    contact.recent_posts_json = json.dumps([{
        "text": p.text, "posted_at": p.posted_at, "url": p.url,
        "likes": p.likes, "comments": p.comments,
    } for p in posts])
    contact.posts_fetched_at = datetime.now(timezone.utc)
    await db.commit()
    return {"posts_count": len(posts), "fetched_at": contact.posts_fetched_at.isoformat()}


@router.post("/contacts/{contact_id}/refresh-linkedin-profile")
async def refresh_contact_linkedin_profile(
    contact_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Pull a full LinkedIn profile via Netrows /people/profile-by-url
    (1 credit). Auto-fills empty title / first_name / last_name on the
    contact and caches the full payload for future renders."""
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if not contact.linkedin_url:
        raise HTTPException(status_code=400, detail="Contact has no LinkedIn URL on file")
    nr_key = await get_netrows_api_key(db)
    if not nr_key:
        raise HTTPException(status_code=400, detail="Data enrichment not configured")

    from app.services.netrows_enrichment import person_profile_by_url
    profile = await person_profile_by_url(contact.linkedin_url, nr_key)
    if profile is None:
        return {"found": False, "message": "Profile not found or rate-limited"}

    # Auto-fill empty fields — never overwrite manually-entered data
    filled = []
    if profile.current_title and not (contact.title or "").strip():
        contact.title = profile.current_title
        filled.append("title")
    if profile.full_name and not (contact.first_name or "").strip() and not (contact.last_name or "").strip():
        parts = profile.full_name.split(maxsplit=1)
        contact.first_name = parts[0]
        if len(parts) > 1:
            contact.last_name = parts[1]
        filled.append("name")

    contact.linkedin_profile_json = json.dumps({
        "full_name": profile.full_name,
        "headline": profile.headline,
        "summary": profile.summary,
        "location": profile.location,
        "current_title": profile.current_title,
        "current_company": profile.current_company,
        "linkedin_url": profile.linkedin_url,
        "profile_pic_url": profile.profile_pic_url,
        "skills": profile.skills,
    }, default=str)
    contact.linkedin_profile_fetched_at = datetime.now(timezone.utc)

    try:
        from app.services.credit_meter import meter, make_idem_key
        await meter(
            db, action_type="enrich_netrows",
            idempotency_key=make_idem_key("enrich_netrows", "profile", contact_id,
                                          datetime.now(timezone.utc).timestamp()),
            user_id=user.id, action_ref=f"contact:{contact_id}",
            raw_cost_override_usd=0.0055,
            metadata={"endpoint": "people/profile-by-url"},
        )
    except Exception:
        pass

    await db.commit()
    return {
        "found": True,
        "filled": filled,
        "fetched_at": contact.linkedin_profile_fetched_at.isoformat(),
        "profile": {
            "full_name": profile.full_name,
            "headline": profile.headline,
            "current_title": profile.current_title,
            "current_company": profile.current_company,
            "location": profile.location,
        },
    }


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
        raise HTTPException(status_code=400, detail="Data enrichment not configured")

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
                    content=f"Email found: {found.email} ({found.email_status})"))
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
        raise HTTPException(status_code=400, detail="Email finder not configured")

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


# ============================================================
# Merge contacts — consolidate duplicate person records
#
# Mirrors the Company merge pattern from app/routes/company_routes.py.
# Re-points all child rows to the kept contact, backfills empty fields,
# unions notes, and deletes the duplicates. Admin-only — destructive.
# ============================================================

class MergeContactsRequest(BaseModel):
    keep_id: int
    merge_from_ids: list[int]


# Tables that have a contact_id FK we need to re-point during a merge.
# Activity, GeneratedEmail, Task, TrackingLink, PageView all have contact_id.
_CONTACT_MERGE_REPOINT_TABLES = ["activities", "generated_emails", "tasks", "tracking_links", "page_views"]


@router.post("/contacts/merge")
async def merge_contacts(
    req: MergeContactsRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Merge duplicate contacts into a kept contact.

    Use cases:
      - Same person captured twice (manual + import)
      - Person has two email addresses on different rows
      - Person changed companies — kept on new company, old contact merged in

    What happens:
      1. All child rows on the merge-from contacts are re-pointed:
         contact_id → keep_id AND company_id → keep.company_id (so all
         history follows the consolidated person to whichever company they
         now live on). Activities, GeneratedEmails, Tasks, TrackingLinks,
         PageViews all flow through.
      2. Empty fields on kept contact (phone, linkedin_url, title, notes)
         are backfilled from the first merge-from row that has a non-empty
         value. Populated kept fields are NOT overwritten.
      3. Notes are appended (kept's notes + each merge-from's notes,
         separated by '\\n---\\n') so we don't lose any handwritten context.
      4. The merge-from contacts are deleted.
      5. An Activity is logged on the kept contact's company recording the merge.

    Idempotent against re-runs: if A+B → A and you call again, A is
    unchanged because B no longer exists.
    """
    from sqlalchemy import text as sql_text
    from app.models import Tag

    # Admin-only — destructive
    if user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    if req.keep_id in req.merge_from_ids:
        raise HTTPException(status_code=400, detail="keep_id can't also appear in merge_from_ids")
    if not req.merge_from_ids:
        raise HTTPException(status_code=400, detail="Pass at least one merge_from_id")

    keep = (await db.execute(select(Contact).where(Contact.id == req.keep_id))).scalar_one_or_none()
    if not keep:
        raise HTTPException(status_code=404, detail="keep_id contact not found")

    merge_from = (await db.execute(select(Contact).where(Contact.id.in_(req.merge_from_ids)))).scalars().all()
    if len(merge_from) != len(req.merge_from_ids):
        found = {c.id for c in merge_from}
        missing = [i for i in req.merge_from_ids if i not in found]
        raise HTTPException(status_code=404, detail=f"Some merge_from_ids not found: {missing}")

    # Backfill empty nullable string fields on the kept contact
    backfill_fields = [
        "first_name", "last_name", "title", "email", "phone", "linkedin_url",
        "phone_type", "phone_carrier", "recent_posts_json",
    ]
    backfilled = []
    for f in backfill_fields:
        if not hasattr(keep, f):
            continue
        cur = getattr(keep, f)
        if isinstance(cur, str) and cur.strip():
            continue  # already populated
        if cur not in (None, ""):
            continue
        for src in merge_from:
            v = getattr(src, f, None)
            if v not in (None, ""):
                setattr(keep, f, v)
                backfilled.append(f)
                break

    # Notes: append (don't overwrite). Keeps any handwritten context from any
    # of the merged rows.
    notes_parts: list[str] = []
    if (keep.notes or "").strip():
        notes_parts.append(keep.notes.strip())
    for src in merge_from:
        if (src.notes or "").strip():
            notes_parts.append(f"--- merged from contact #{src.id} ---\n{src.notes.strip()}")
    if notes_parts:
        keep.notes = "\n\n".join(notes_parts)

    # Re-point all child tables. Set BOTH contact_id and company_id so the
    # full activity/sequence/task history follows the kept contact onto
    # their canonical company (matters when the merge-from contacts lived
    # on different companies than keep).
    placeholders = ",".join(f":id{i}" for i in range(len(req.merge_from_ids)))
    base_params = {"keep": req.keep_id, "keep_co": keep.company_id,
                   **{f"id{i}": v for i, v in enumerate(req.merge_from_ids)}}
    repoint_counts: dict[str, int] = {}
    for tbl in _CONTACT_MERGE_REPOINT_TABLES:
        result = await db.execute(
            sql_text(f"UPDATE {tbl} SET contact_id = :keep, company_id = :keep_co WHERE contact_id IN ({placeholders})"),
            base_params,
        )
        repoint_counts[tbl] = result.rowcount or 0

    # Now safe to delete the merge-from contact rows. Cascades nothing
    # because we already moved every child row.
    deleted_descriptors = [f"#{c.id} {(c.full_name or c.email or 'unnamed')}" for c in merge_from]
    for src in merge_from:
        await db.delete(src)

    # Audit Activity on the kept contact's company
    db.add(Activity(
        company_id=keep.company_id,
        contact_id=keep.id,
        user_id=user.id,
        activity_type="contact_merged",
        content=f"Merged {len(merge_from)} duplicate contact(s) into {keep.full_name or keep.email or '#' + str(keep.id)}: {', '.join(deleted_descriptors)}",
        metadata_json=json.dumps({
            "merged_from_ids": req.merge_from_ids,
            "merged_from_descriptors": deleted_descriptors,
            "repoint_counts": repoint_counts,
            "backfilled_fields": backfilled,
        }),
    ))

    await db.commit()
    await db.refresh(keep)
    return {
        "kept_id": keep.id,
        "kept_name": keep.full_name or keep.email,
        "merged_count": len(merge_from),
        "merged_descriptors": deleted_descriptors,
        "repoint_counts": repoint_counts,
        "backfilled_fields": backfilled,
    }
