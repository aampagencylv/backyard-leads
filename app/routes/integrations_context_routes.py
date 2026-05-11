"""Shared 'tell me everything you know about this email/domain' endpoint.

Backs BOTH the Missive sidebar AND the Chrome extension. Each client
ships its own UI shell but pulls the same context payload — so feature
parity is automatic and we keep one source of truth.

Auth: bearer JWT (any signed-in Prospector user). The Missive sidebar
stashes the user's JWT in Missive's per-integration storage after a
one-time login flow (see /integrations/missive/auth). The Chrome
extension reads it from prospector.bymp.com's localStorage.

Lookup precedence:
  1. email — exact match on contacts.email (case-insensitive)
  2. domain — companies whose website host matches the domain
  3. company_id — direct lookup

Returns a single Contact + its Company + recent Activities + the
current sequence position + audit-report status + lead score.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, or_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import (
    User, Company, Contact, Activity, GeneratedEmail, AuditReportModel,
)
from app.config import settings

router = APIRouter(prefix="/api/integrations", tags=["integrations"])
log = logging.getLogger("bmp.integrations_context")


def _domain_from(value: str) -> str:
    """Pull the host out of an email or URL. Lowercased, no scheme, no path."""
    if not value:
        return ""
    s = value.strip().lower()
    if "@" in s:
        s = s.split("@", 1)[1]
    if s.startswith("http://"):
        s = s[7:]
    if s.startswith("https://"):
        s = s[8:]
    if s.startswith("www."):
        s = s[4:]
    s = s.split("/", 1)[0]
    return s


@router.get("/context")
async def get_context(
    email: Optional[str] = Query(None, description="Email address — primary lookup"),
    domain: Optional[str] = Query(None, description="Company domain — fallback lookup"),
    company_id: Optional[int] = Query(None, description="Direct company id"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Resolve a contact/company/activity bundle for the given lookup
    keys. Any one of email/domain/company_id is enough. Returns a
    compact JSON payload designed to render in a sidebar — keep it
    under a few hundred lines worth of UI."""
    contact: Optional[Contact] = None
    company: Optional[Company] = None

    # 1. Direct company id
    if company_id:
        company = (await db.execute(
            select(Company).where(Company.id == company_id)
        )).scalar_one_or_none()

    # 2. Email — most reliable
    if not contact and email:
        e = email.strip().lower()
        contact = (await db.execute(
            select(Contact).where(func.lower(Contact.email) == e)
        )).scalars().first()
        if contact and not company:
            company = (await db.execute(
                select(Company).where(Company.id == contact.company_id)
            )).scalar_one_or_none()

    # 3. Domain — fallback when we don't have the exact contact yet
    if not company and (domain or email):
        dom = _domain_from(domain or email or "")
        if dom:
            # match against company.website (host may have scheme/path)
            # SQLite doesn't have a host extractor, so we just LIKE-match
            company = (await db.execute(
                select(Company).where(
                    or_(
                        Company.website.ilike(f"%{dom}%"),
                        Company.domain.ilike(f"%{dom}%") if hasattr(Company, "domain") else Company.website.ilike(f"%{dom}%"),
                    )
                )
            )).scalars().first()
            # If we matched a company by domain, see if any of its
            # contacts match the supplied email
            if company and email and not contact:
                e = email.strip().lower()
                contact = (await db.execute(
                    select(Contact).where(
                        Contact.company_id == company.id,
                        func.lower(Contact.email) == e,
                    )
                )).scalars().first()

    if not contact and not company:
        return {
            "found": False,
            "lookup": {"email": email, "domain": domain, "company_id": company_id},
        }

    # Activities — most recent 8 for the company. Cheap, useful inline.
    activities = []
    if company:
        rows = (await db.execute(
            select(Activity).where(Activity.company_id == company.id)
            .order_by(desc(Activity.created_at)).limit(8)
        )).scalars().all()
        activities = [{
            "id": a.id,
            "type": a.activity_type,
            "content": a.content or "",
            "at": a.created_at.isoformat() if a.created_at else None,
        } for a in rows]

    # Sequence position — next unsent step for this contact (if any)
    sequence = None
    if contact:
        next_step = (await db.execute(
            select(GeneratedEmail).where(
                GeneratedEmail.contact_id == contact.id,
                GeneratedEmail.is_sent == False,
                GeneratedEmail.paused_at.is_(None),
                GeneratedEmail.skipped_at.is_(None),
            ).order_by(GeneratedEmail.sequence_order.asc(), GeneratedEmail.scheduled_send_at.asc()).limit(1)
        )).scalars().first()
        # Total steps for this contact (helps render "step 3 of 13")
        total = (await db.execute(
            select(func.count(GeneratedEmail.id)).where(
                GeneratedEmail.contact_id == contact.id,
            )
        )).scalar() or 0
        sent = (await db.execute(
            select(func.count(GeneratedEmail.id)).where(
                GeneratedEmail.contact_id == contact.id,
                GeneratedEmail.is_sent == True,
            )
        )).scalar() or 0
        sequence = {
            "total_steps": int(total),
            "sent_steps": int(sent),
            "next_step": {
                "id": next_step.id,
                "type": next_step.step_type or "email",
                "subject": next_step.subject,
                "scheduled_at": next_step.scheduled_send_at.isoformat() if next_step and next_step.scheduled_send_at else None,
                "order": next_step.sequence_order,
            } if next_step else None,
        }

    # Audit report — exists / score / URL
    audit = None
    if company:
        ar = (await db.execute(
            select(AuditReportModel).where(AuditReportModel.company_id == company.id)
        )).scalar_one_or_none()
        if ar:
            audit_base = settings.audit_public_url.rstrip("/")
            audit = {
                "token": ar.token,
                "url": f"{audit_base}/report/{ar.token}",
                "grade": ar.overall_grade,
                "ai_findability_score": ar.ai_findability_score,
                "view_count": ar.view_count or 0,
                "generated_at": ar.generated_at.isoformat() if ar.generated_at else None,
            }

    return {
        "found": True,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "lookup": {"email": email, "domain": domain, "company_id": company_id},
        "contact": _serialize_contact(contact) if contact else None,
        "company": _serialize_company(company) if company else None,
        "sequence": sequence,
        "audit": audit,
        "activities": activities,
        "app_url": settings.public_url.rstrip("/"),
    }


def _serialize_contact(c: Contact) -> dict:
    return {
        "id": c.id,
        "first_name": c.first_name or "",
        "last_name": c.last_name or "",
        "full_name": f"{(c.first_name or '').strip()} {(c.last_name or '').strip()}".strip(),
        "title": c.title or "",
        "email": c.email or "",
        "phone": c.phone or "",
        "linkedin_url": c.linkedin_url or "",
        "email_status": c.email_status or "unknown",
        "unsubscribed_at": c.unsubscribed_at.isoformat() if c.unsubscribed_at else None,
        "do_not_text": bool(getattr(c, "do_not_text", False)),
        "is_primary": bool(getattr(c, "is_primary", False)),
    }


def _serialize_company(c: Company) -> dict:
    return {
        "id": c.id,
        "name": c.name or "",
        "status": c.status or "new",
        "city": getattr(c, "city", "") or "",
        "state": getattr(c, "state", "") or "",
        "website": getattr(c, "website", "") or "",
        "business_type": getattr(c, "business_type", "") or "",
        "rating": getattr(c, "rating", 0) or 0,
        "review_count": getattr(c, "review_count", 0) or 0,
        "lead_score": getattr(c, "lead_score", 0) or 0,
        "lead_score_tier": getattr(c, "lead_score_tier", "cold") or "cold",
    }
