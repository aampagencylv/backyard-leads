"""
Public REST API surface (v1).

Auth: X-API-Key header. Keys are personal — every call acts as the
key's owner with their role + scoping. No public unauthenticated
surface; rate-limiting is per-key (TODO — not yet enforced; logged
in audit trail).

Versioned at /api/v1/* so future breaking changes can land at /v2/.

v1 endpoints:
  GET  /api/v1/companies            list (limit, status, search)
  POST /api/v1/companies            create / upsert by domain
  GET  /api/v1/companies/{id}       full record
  PATCH /api/v1/companies/{id}      partial update (incl. custom_fields)

  GET  /api/v1/contacts/{id}        full record
  POST /api/v1/contacts             create / upsert by email
  PATCH /api/v1/contacts/{id}       partial update (incl. custom_fields)

Webhook integrations (incoming) live under /api/v1/webhooks/* but are
authenticated separately (HMAC signature verification per source).
"""
from __future__ import annotations
import json
import secrets as _secrets
from typing import Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from pydantic import BaseModel

from app.tenancy import get_tenant_db
from app.models import User, Company, Contact, Deal, Activity, CustomFieldDefinition
from app.auth import get_user_from_api_key
from app.scoping import scope_companies, check_company_access, check_contact_access
from app.services.domain_utils import normalize_domain

router = APIRouter(prefix="/api/v1", tags=["public-api-v1"])


# ============================================================
# Serializers — mirror the internal /api/companies shape but stable
# ============================================================

def _company_payload(c: Company) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "phone": c.phone,
        "website": c.website,
        "domain": c.domain,
        "address": c.address,
        "city": c.city,
        "state": c.state,
        "rating": c.rating,
        "review_count": c.review_count,
        "business_type": c.business_type,
        "status": c.status,
        "industry": c.industry,
        "employee_count": c.employee_count,
        "company_size": c.company_size,
        "founded": c.founded,
        "linkedin_url": c.linkedin_url,
        "facebook_url": c.facebook_url,
        "instagram_url": c.instagram_url,
        "youtube_url": c.youtube_url,
        "tiktok_url": c.tiktok_url,
        "lead_score": c.lead_score or 0,
        "lead_score_tier": c.lead_score_tier or "cold",
        "custom_fields": json.loads(c.custom_fields_json) if c.custom_fields_json else {},
        "assigned_to": c.assigned_to,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _contact_payload(c: Contact) -> dict:
    return {
        "id": c.id,
        "company_id": c.company_id,
        "first_name": c.first_name,
        "last_name": c.last_name,
        "title": c.title,
        "email": c.email,
        "phone": c.phone,
        "phone_type": c.phone_type,
        "linkedin_url": c.linkedin_url,
        "is_primary": c.is_primary,
        "email_status": c.email_status,
        "unsubscribed_at": c.unsubscribed_at.isoformat() if c.unsubscribed_at else None,
        "do_not_text": bool(c.do_not_text),
        "custom_fields": json.loads(c.custom_fields_json) if c.custom_fields_json else {},
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


# ============================================================
# Companies — list / get / create / update
# ============================================================

@router.get("/companies")
async def list_companies(
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_user_from_api_key),
):
    """List companies. Honors the caller's role-based scoping —
    sales_rep sees only their assigned companies; admin sees all."""
    q = scope_companies(select(Company), user)
    if status:
        q = q.where(Company.status == status)
    if search:
        like = f"%{search}%"
        q = q.where(or_(Company.name.ilike(like), Company.website.ilike(like)))
    q = q.order_by(Company.id.desc()).limit(min(max(limit, 1), 200)).offset(max(offset, 0))
    rows = (await db.execute(q)).scalars().all()
    return {
        "data": [_company_payload(c) for c in rows],
        "limit": limit, "offset": offset, "count": len(rows),
    }


@router.get("/companies/{company_id}")
async def get_company(
    company_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_user_from_api_key),
):
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company or not check_company_access(company, user):
        raise HTTPException(status_code=404, detail="Company not found")
    return _company_payload(company)


class CreateCompanyAPI(BaseModel):
    """POST /api/v1/companies body. Upserts by canonical domain when
    `website` is set + the domain is already in our DB; otherwise creates."""
    name: str
    website: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    business_type: Optional[str] = None
    custom_fields: Optional[dict] = None


@router.post("/companies", status_code=201)
async def create_company(
    req: CreateCompanyAPI,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_user_from_api_key),
):
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    new_domain = normalize_domain(req.website)
    existing = None
    if new_domain:
        existing = (await db.execute(
            select(Company).where(Company.domain == new_domain)
        )).scalars().first()
    if existing:
        # Upsert: fill blank fields only — never clobber existing data
        if req.phone and not existing.phone: existing.phone = req.phone
        if req.address and not existing.address: existing.address = req.address
        if req.city and not existing.city: existing.city = req.city
        if req.state and not existing.state: existing.state = req.state
        if req.business_type and not existing.business_type: existing.business_type = req.business_type
        company = existing
        created = False
    else:
        company = Company(
            name=req.name.strip(),
            website=(req.website or "").strip() or None,
            domain=new_domain,
            phone=(req.phone or "").strip() or None,
            address=(req.address or "").strip() or None,
            city=(req.city or "").strip() or None,
            state=(req.state or "").strip() or None,
            business_type=(req.business_type or "").strip() or None,
            status="new",
            assigned_to=user.id,
        )
        db.add(company)
        await db.flush()
        created = True

    # Custom fields — only accept keys that match active definitions
    if req.custom_fields:
        valid = set((await db.execute(
            select(CustomFieldDefinition.key).where(
                CustomFieldDefinition.entity_type == "company",
                CustomFieldDefinition.is_active == True,
            )
        )).scalars().all())
        try:
            current_cf = json.loads(company.custom_fields_json) if company.custom_fields_json else {}
        except Exception:
            current_cf = {}
        for k, v in req.custom_fields.items():
            if k in valid and v not in (None, ""):
                current_cf[k] = v
        company.custom_fields_json = json.dumps(current_cf) if current_cf else None

    await db.commit()
    await db.refresh(company)

    # Fire webhook (created OR upserted-with-changes both count as 'company.created'
    # for v1; refine to company.updated when we add diff tracking)
    try:
        if created:
            from app.services.webhook_dispatch import dispatch_event
            await dispatch_event(db, "company.created", _company_payload(company))
    except Exception:
        pass

    return {"created": created, "company": _company_payload(company)}


class UpdateCompanyAPI(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    business_type: Optional[str] = None
    status: Optional[str] = None
    custom_fields: Optional[dict] = None


@router.patch("/companies/{company_id}")
async def patch_company(
    company_id: int,
    req: UpdateCompanyAPI,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_user_from_api_key),
):
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company or not check_company_access(company, user):
        raise HTTPException(status_code=404, detail="Company not found")
    for field_name in ("name", "phone", "website", "address", "city", "state",
                        "business_type", "status"):
        val = getattr(req, field_name)
        if val is not None:
            setattr(company, field_name, val.strip() or None)
    if req.website is not None:
        company.domain = normalize_domain(req.website)
    if req.custom_fields:
        valid = set((await db.execute(
            select(CustomFieldDefinition.key).where(
                CustomFieldDefinition.entity_type == "company",
                CustomFieldDefinition.is_active == True,
            )
        )).scalars().all())
        try:
            current_cf = json.loads(company.custom_fields_json) if company.custom_fields_json else {}
        except Exception:
            current_cf = {}
        for k, v in req.custom_fields.items():
            if k in valid:
                if v in (None, ""):
                    current_cf.pop(k, None)
                else:
                    current_cf[k] = v
        company.custom_fields_json = json.dumps(current_cf) if current_cf else None
    await db.commit()
    return _company_payload(company)


# ============================================================
# Contacts — get / create / update
# ============================================================

@router.get("/contacts/{contact_id}")
async def get_contact(
    contact_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_user_from_api_key),
):
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact or not await check_contact_access(contact, user, db):
        raise HTTPException(status_code=404, detail="Contact not found")
    return _contact_payload(contact)


class CreateContactAPI(BaseModel):
    """POST /api/v1/contacts. Either company_id OR company_domain must
    be provided; upserts by email within that company when present."""
    company_id: Optional[int] = None
    company_domain: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    title: Optional[str] = None
    linkedin_url: Optional[str] = None
    is_primary: bool = False
    custom_fields: Optional[dict] = None


@router.post("/contacts", status_code=201)
async def create_contact(
    req: CreateContactAPI,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_user_from_api_key),
):
    if not req.company_id and not req.company_domain:
        raise HTTPException(status_code=400, detail="company_id or company_domain is required")

    company = None
    if req.company_id:
        company = (await db.execute(select(Company).where(Company.id == req.company_id))).scalar_one_or_none()
    elif req.company_domain:
        cd = normalize_domain(req.company_domain)
        if cd:
            company = (await db.execute(select(Company).where(Company.domain == cd))).scalars().first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if not check_company_access(company, user):
        raise HTTPException(status_code=403, detail="Access denied for this company")

    # Upsert by email
    existing = None
    if req.email:
        existing = (await db.execute(
            select(Contact).where(Contact.company_id == company.id, Contact.email == req.email.lower())
        )).scalar_one_or_none()

    if existing:
        if req.first_name and not existing.first_name: existing.first_name = req.first_name
        if req.last_name and not existing.last_name: existing.last_name = req.last_name
        if req.title and not existing.title: existing.title = req.title
        if req.phone and not existing.phone: existing.phone = req.phone
        if req.linkedin_url and not existing.linkedin_url: existing.linkedin_url = req.linkedin_url
        contact = existing
        created = False
    else:
        contact = Contact(
            company_id=company.id,
            first_name=(req.first_name or "").strip(),
            last_name=(req.last_name or "").strip(),
            email=(req.email or "").strip().lower() or None,
            phone=(req.phone or "").strip() or None,
            title=(req.title or "").strip() or None,
            linkedin_url=(req.linkedin_url or "").strip() or None,
            is_primary=bool(req.is_primary),
            unsubscribe_token=_secrets.token_urlsafe(24),
        )
        db.add(contact)
        await db.flush()
        created = True

    if req.custom_fields:
        valid = set((await db.execute(
            select(CustomFieldDefinition.key).where(
                CustomFieldDefinition.entity_type == "contact",
                CustomFieldDefinition.is_active == True,
            )
        )).scalars().all())
        try:
            current_cf = json.loads(contact.custom_fields_json) if contact.custom_fields_json else {}
        except Exception:
            current_cf = {}
        for k, v in req.custom_fields.items():
            if k in valid and v not in (None, ""):
                current_cf[k] = v
        contact.custom_fields_json = json.dumps(current_cf) if current_cf else None

    await db.commit()
    await db.refresh(contact)

    try:
        if created:
            from app.services.webhook_dispatch import dispatch_event
            await dispatch_event(db, "contact.created", _contact_payload(contact))
    except Exception:
        pass

    return {"created": created, "contact": _contact_payload(contact)}


class UpdateContactAPI(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    title: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    linkedin_url: Optional[str] = None
    is_primary: Optional[bool] = None
    custom_fields: Optional[dict] = None


@router.patch("/contacts/{contact_id}")
async def patch_contact(
    contact_id: int,
    req: UpdateContactAPI,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_user_from_api_key),
):
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact or not await check_contact_access(contact, user, db):
        raise HTTPException(status_code=404, detail="Contact not found")
    for field_name in ("first_name", "last_name", "title", "email", "phone", "linkedin_url"):
        val = getattr(req, field_name)
        if val is not None:
            setattr(contact, field_name, val.strip() or None)
    if req.is_primary is not None:
        contact.is_primary = bool(req.is_primary)
    if req.custom_fields:
        valid = set((await db.execute(
            select(CustomFieldDefinition.key).where(
                CustomFieldDefinition.entity_type == "contact",
                CustomFieldDefinition.is_active == True,
            )
        )).scalars().all())
        try:
            current_cf = json.loads(contact.custom_fields_json) if contact.custom_fields_json else {}
        except Exception:
            current_cf = {}
        for k, v in req.custom_fields.items():
            if k in valid:
                if v in (None, ""):
                    current_cf.pop(k, None)
                else:
                    current_cf[k] = v
        contact.custom_fields_json = json.dumps(current_cf) if current_cf else None
    await db.commit()
    return _contact_payload(contact)
