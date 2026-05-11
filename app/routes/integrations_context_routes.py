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

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func, or_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import (
    User, Company, Contact, Activity, GeneratedEmail, AuditReportModel,
)
from app.config import settings
from app.services import missive_client

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
    conversation_id: Optional[str] = Query(None, description="Missive conversation ID — persisted onto the contact so status-change hooks know which thread to write back to"),
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
            "team_emails": sorted(await missive_client.team_emails()),
            "missive_configured": missive_client.is_configured(),
        }

    # Link the Missive conversation to this contact so later status-
    # change hooks can write back to the right thread. Only updates
    # when the value actually changes — keeps `seen_at` honest.
    if contact and conversation_id:
        cid = conversation_id.strip()[:64]
        if cid and contact.missive_conversation_id != cid:
            contact.missive_conversation_id = cid
            contact.missive_conversation_seen_at = datetime.now(timezone.utc)
            await db.commit()

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

    # Recent emails sent to this contact (last 5) — gives the BDR a
    # one-glance view of the sequence touchpoints they've already
    # received. Sorted newest-first by sent_at.
    recent_emails = []
    last_opened_at = None
    last_clicked_at = None
    if contact:
        em_rows = (await db.execute(
            select(GeneratedEmail).where(
                GeneratedEmail.contact_id == contact.id,
            ).order_by(desc(GeneratedEmail.sent_at.is_(None)), desc(GeneratedEmail.sent_at), desc(GeneratedEmail.id)).limit(5)
        )).scalars().all()
        recent_emails = [{
            "id": e.id,
            "subject": e.subject,
            "step_type": e.step_type or "email",
            "sent_at": e.sent_at.isoformat() if e.sent_at else None,
            "delivered_at": e.delivered_at.isoformat() if e.delivered_at else None,
            "opened_at": e.opened_at.isoformat() if e.opened_at else None,
            "open_count": int(e.open_count or 0),
            "bounced_at": e.bounced_at.isoformat() if e.bounced_at else None,
            "complained_at": e.complained_at.isoformat() if e.complained_at else None,
            "is_sent": bool(e.is_sent),
        } for e in em_rows]
        # Newest open / click — pulled from the activities feed since
        # clicks live on TrackingLink rows, not GeneratedEmail.
        last_opened_at_row = (await db.execute(
            select(GeneratedEmail.opened_at).where(
                GeneratedEmail.contact_id == contact.id,
                GeneratedEmail.opened_at.isnot(None),
            ).order_by(desc(GeneratedEmail.opened_at)).limit(1)
        )).scalar_one_or_none()
        last_opened_at = last_opened_at_row.isoformat() if last_opened_at_row else None
        last_click_act = (await db.execute(
            select(Activity).where(
                Activity.contact_id == contact.id,
                Activity.activity_type == "email_clicked",
            ).order_by(desc(Activity.created_at)).limit(1)
        )).scalar_one_or_none()
        last_clicked_at = last_click_act.created_at.isoformat() if last_click_act and last_click_act.created_at else None

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
        "recent_emails": recent_emails,
        "last_opened_at": last_opened_at,
        "last_clicked_at": last_clicked_at,
        "app_url": settings.public_url.rstrip("/"),
        "team_emails": sorted(await missive_client.team_emails()),
        "missive_configured": missive_client.is_configured(),
        "missive_conversation_linked": bool(contact and contact.missive_conversation_id),
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


# ============================================================
# Sidebar action endpoints — small mutations the Missive iframe (and
# the upcoming Chrome extension) calls into.
# ============================================================


class LogNoteRequest(BaseModel):
    contact_id: int
    company_id: int
    text: str
    activity_type: str = "note"  # note | call_logged | meeting | custom


@router.post("/sidebar/log-activity")
async def sidebar_log_activity(
    req: LogNoteRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Append an Activity row from the sidebar. Used for inline 'Log a
    note' and 'Log a call' buttons."""
    body = (req.text or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="text is required")
    if len(body) > 4000:
        body = body[:4000]
    valid_types = {"note", "call", "call_logged", "meeting", "custom"}
    atype = req.activity_type if req.activity_type in valid_types else "note"
    act = Activity(
        company_id=req.company_id,
        contact_id=req.contact_id,
        user_id=user.id,
        activity_type=atype,
        content=body,
    )
    db.add(act)
    await db.commit()
    await db.refresh(act)
    return {"id": act.id, "type": act.activity_type, "at": act.created_at.isoformat() if act.created_at else None}


class QuickAddRequest(BaseModel):
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    company_name: Optional[str] = None
    title: Optional[str] = None


@router.post("/sidebar/quick-add")
async def sidebar_quick_add(
    req: QuickAddRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Minimal-form add a prospect from the sidebar. Creates a Company
    (matched on domain if one already exists, else a stub) and a
    Contact attached to it. Returns the new IDs so the sidebar can
    immediately re-fetch context."""
    e = (req.email or "").strip().lower()
    if not e or "@" not in e:
        raise HTTPException(status_code=400, detail="valid email required")
    dom = e.split("@", 1)[1]

    # Existing contact? Don't dupe.
    existing = (await db.execute(
        select(Contact).where(func.lower(Contact.email) == e)
    )).scalars().first()
    if existing:
        return {"created": False, "contact_id": existing.id, "company_id": existing.company_id}

    # Match company by domain if we have it
    company = (await db.execute(
        select(Company).where(Company.website.ilike(f"%{dom}%"))
    )).scalars().first()
    if not company:
        company = Company(
            name=(req.company_name or dom).strip()[:200],
            website=f"https://{dom}",
            status="new",
        )
        db.add(company)
        await db.flush()  # need company.id

    fn = (req.first_name or "").strip() or ((req.full_name or "").split(" ", 1)[0] if req.full_name else "")
    ln = (req.last_name or "").strip() or ((req.full_name or "").split(" ", 1)[1] if (req.full_name and " " in req.full_name) else "")
    contact = Contact(
        company_id=company.id,
        first_name=fn[:80],
        last_name=ln[:80],
        title=(req.title or "")[:255],
        email=e,
        email_status="unknown",
    )
    db.add(contact)
    db.add(Activity(
        company_id=company.id,
        contact_id=None,
        user_id=user.id,
        activity_type="contact_added",
        content=f"Added via sidebar quick-add: {e}",
    ))
    await db.commit()
    await db.refresh(contact)
    return {"created": True, "contact_id": contact.id, "company_id": company.id}


class MissiveTagSyncRequest(BaseModel):
    contact_id: int
    conversation_id: str


@router.post("/sidebar/missive-sync-tag")
async def sidebar_missive_sync_tag(
    req: MissiveTagSyncRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Apply the right Missive shared label for the contact's current
    company status. Drops a small explanatory comment in the
    conversation at the same time so teammates see why the label
    changed. Safe to call repeatedly — Missive's add_shared_labels is
    idempotent.

    Also persists the conversation_id onto the contact so any future
    server-side status change can auto-sync without a manual click."""
    if not missive_client.is_configured():
        raise HTTPException(status_code=400, detail="Missive API token not configured")

    contact = (await db.execute(
        select(Contact).where(Contact.id == req.contact_id)
    )).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="contact not found")

    company = (await db.execute(
        select(Company).where(Company.id == contact.company_id)
    )).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="company not found")

    cid = req.conversation_id.strip()[:64]
    if not cid:
        raise HTTPException(status_code=400, detail="conversation_id required")

    # Persist the linkage so server-side status hooks know the thread later
    if contact.missive_conversation_id != cid:
        contact.missive_conversation_id = cid
        contact.missive_conversation_seen_at = datetime.now(timezone.utc)
        await db.commit()

    contact_name = f"{(contact.first_name or '').strip()} {(contact.last_name or '').strip()}".strip()
    result = await missive_client.sync_status_label(
        conversation_id=cid,
        new_status=(company.status or "").strip(),
        contact_name=contact_name or (contact.email or ""),
        company_name=company.name or "",
        actor=f"{user.first_name or user.email}",
    )
    if "_error" in result:
        # Missive write failed but DB linkage is updated — return the
        # error so the UI can show a toast. Not a 500 — this is a soft
        # write that we never want to block on.
        return {"ok": False, "error": result["_error"]}
    return {
        "ok": True,
        "status_applied": company.status,
        "label_name": missive_client.STATUS_TO_LABEL_NAME.get(company.status, ""),
    }


class SendNextStepRequest(BaseModel):
    contact_id: int


@router.post("/sidebar/send-next-step")
async def sidebar_send_next_step(
    req: SendNextStepRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Find the next unsent step for this contact and fire it now via
    the sequence engine. No-op when there's no pending step."""
    next_step = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.contact_id == req.contact_id,
            GeneratedEmail.is_sent == False,
            GeneratedEmail.paused_at.is_(None),
            GeneratedEmail.skipped_at.is_(None),
        ).order_by(GeneratedEmail.sequence_order.asc(), GeneratedEmail.id.asc()).limit(1)
    )).scalar_one_or_none()
    if not next_step:
        return {"fired": False, "reason": "no pending step"}

    # Defer to the sequence engine so all the same gating/skip-rules
    # apply that auto-execution uses (deliverability caps, opt-out
    # checks, audit URL weaving, etc).
    try:
        from app.services.sequence_engine import execute_step_now
        result = await execute_step_now(db, next_step.id, triggered_by_user_id=user.id)
        return {"fired": True, "step_id": next_step.id, "result": result}
    except ImportError:
        # execute_step_now hasn't been added yet — fall back to a
        # direct send via send_email. This will be wired properly in
        # a follow-up.
        return {"fired": False, "reason": "manual trigger not yet wired in sequence engine"}


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
