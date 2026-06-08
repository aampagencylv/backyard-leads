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
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func, or_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.tenancy import get_tenant_db
from app.models import (
    User, Company, Contact, Activity, GeneratedEmail, AuditReportModel, Task,
)
from app.config import settings
from app.services import missive_client

router = APIRouter(prefix="/api/integrations", tags=["integrations"])
log = logging.getLogger("bmp.integrations_context")


def _extract_linkedin_slug(url: str) -> str:
    """Pull the /in/<slug>/ slug out of a LinkedIn URL. Handles the
    public-profile URL shape; returns empty when the URL doesn't
    contain /in/.

    Examples:
      https://www.linkedin.com/in/jane-doe-123/      → "jane-doe-123"
      https://linkedin.com/in/jane-doe/?utm_source=… → "jane-doe"
      https://linkedin.com/sales/lead/4567/          → ""  (no slug)
    """
    if not url:
        return ""
    s = url.strip().lower()
    if "/in/" not in s:
        return ""
    after = s.split("/in/", 1)[1]
    slug = after.split("/", 1)[0].split("?", 1)[0].strip()
    return slug


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
    linkedin: Optional[str] = Query(None, description="LinkedIn profile URL — used by the Chrome extension's LinkedIn content script"),
    company_id: Optional[int] = Query(None, description="Direct company id"),
    conversation_id: Optional[str] = Query(None, description="Missive conversation ID — persisted onto the contact so status-change hooks know which thread to write back to"),
    db: AsyncSession = Depends(get_tenant_db),
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

    # 2.5. LinkedIn URL — fuzzy match on Contact.linkedin_url.
    # LinkedIn URLs can come in many shapes (/in/<slug>/, /in/<slug>/?...,
    # /sales/lead/<id>/, etc), so we match on the slug substring when
    # we recognize it, else on the bare URL substring.
    if not contact and linkedin:
        slug = _extract_linkedin_slug(linkedin)
        if slug:
            contact = (await db.execute(
                select(Contact).where(Contact.linkedin_url.ilike(f"%/in/{slug}%"))
            )).scalars().first()
        if not contact:
            # Fallback: substring against the raw input
            li = linkedin.strip().lower()
            if "linkedin.com" in li:
                contact = (await db.execute(
                    select(Contact).where(func.lower(Contact.linkedin_url).contains(li[:200]))
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
            "lookup": {"email": email, "domain": domain, "linkedin": linkedin, "company_id": company_id},
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
        # POST-CUTOVER: query BOTH legacy generated_emails AND new-engine
        # actions for this contact's sequence counts + next step. Kevin's
        # MCP tool surface reads from this endpoint, so without the union
        # Kevin reports "no sequence" or "0 steps" for every engine-
        # enrolled contact (i.e. virtually all of them today).
        from sqlalchemy import text as _sa_text
        next_step = (await db.execute(
            select(GeneratedEmail).where(
                GeneratedEmail.contact_id == contact.id,
                GeneratedEmail.is_sent == False,
                GeneratedEmail.paused_at.is_(None),
                GeneratedEmail.skipped_at.is_(None),
            ).order_by(GeneratedEmail.sequence_order.asc(), GeneratedEmail.scheduled_send_at.asc()).limit(1)
        )).scalars().first()

        # Engine action equivalents
        engine_next = (await db.execute(_sa_text("""
            SELECT a.id, ct.code AS channel, a.subject, a.scheduled_at,
                   ROW_NUMBER() OVER (ORDER BY a.scheduled_at, a.id) AS step_order
            FROM actions a JOIN channel_types ct ON ct.id = a.channel_id
            WHERE a.contact_id = :c AND a.status = 'scheduled'
            ORDER BY a.scheduled_at ASC LIMIT 1
        """), {"c": contact.id})).first()
        engine_total = (await db.execute(_sa_text(
            "SELECT COUNT(*) FROM actions WHERE contact_id = :c"
        ), {"c": contact.id})).scalar() or 0
        engine_sent = (await db.execute(_sa_text(
            "SELECT COUNT(*) FROM actions WHERE contact_id = :c AND status = 'sent'"
        ), {"c": contact.id})).scalar() or 0

        legacy_total = (await db.execute(
            select(func.count(GeneratedEmail.id)).where(
                GeneratedEmail.contact_id == contact.id,
            )
        )).scalar() or 0
        legacy_sent = (await db.execute(
            select(func.count(GeneratedEmail.id)).where(
                GeneratedEmail.contact_id == contact.id,
                GeneratedEmail.is_sent == True,
            )
        )).scalar() or 0

        # Pick whichever next-step fires first chronologically
        legacy_due = (next_step.scheduled_send_at if next_step and next_step.scheduled_send_at else None)
        engine_due = engine_next.scheduled_at if engine_next else None
        chosen_next = None
        if next_step and engine_next:
            chosen_next = (
                {"_src": "engine"} if (engine_due and (not legacy_due or engine_due < legacy_due))
                else {"_src": "legacy"}
            )
        elif next_step:
            chosen_next = {"_src": "legacy"}
        elif engine_next:
            chosen_next = {"_src": "engine"}

        next_payload = None
        if chosen_next and chosen_next["_src"] == "legacy" and next_step:
            next_payload = {
                "id": next_step.id,
                "type": next_step.step_type or "email",
                "subject": next_step.subject,
                "scheduled_at": next_step.scheduled_send_at.isoformat() if next_step.scheduled_send_at else None,
                "order": next_step.sequence_order,
            }
        elif chosen_next and chosen_next["_src"] == "engine" and engine_next:
            step_type_map = {"email": "email", "sms": "imessage", "call_task": "call",
                             "linkedin": "linkedin", "manual": "manual", "wait": "wait"}
            next_payload = {
                "id": int(engine_next.id),
                "type": step_type_map.get(engine_next.channel, engine_next.channel),
                "subject": engine_next.subject,
                "scheduled_at": engine_next.scheduled_at.isoformat() if engine_next.scheduled_at else None,
                "order": int(engine_next.step_order),
            }

        sequence = {
            "total_steps": int(legacy_total + engine_total),
            "sent_steps": int(legacy_sent + engine_sent),
            "next_step": next_payload,
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

    # Notes + call-logs pinned for prominent display at the top of the
    # sidebar — these are activity_type IN ('note', 'call', 'call_logged',
    # 'meeting'). Keep the full activity feed too (above) for everything
    # else. Cap at 6 most recent.
    pinned_notes = []
    if company:
        note_rows = (await db.execute(
            select(Activity).where(
                Activity.company_id == company.id,
                Activity.activity_type.in_(["note", "call", "call_logged", "meeting"]),
            ).order_by(desc(Activity.created_at)).limit(6)
        )).scalars().all()
        pinned_notes = [{
            "id": a.id,
            "type": a.activity_type,
            "content": a.content or "",
            "at": a.created_at.isoformat() if a.created_at else None,
            "user_id": a.user_id,
        } for a in note_rows]

    # Other contacts at the same company — so a BDR looking at Linda
    # can see "Mark, Sarah, +3 others" at AAMP at a glance.
    other_contacts = []
    if company:
        oc_rows = (await db.execute(
            select(Contact).where(
                Contact.company_id == company.id,
                Contact.id != (contact.id if contact else -1),
            ).order_by(desc(Contact.is_primary), Contact.id).limit(8)
        )).scalars().all()
        other_contacts = [{
            "id": c2.id,
            "full_name": f"{(c2.first_name or '').strip()} {(c2.last_name or '').strip()}".strip(),
            "title": c2.title or "",
            "email": c2.email or "",
            "phone": c2.phone or "",
            "is_primary": bool(c2.is_primary),
        } for c2 in oc_rows]

    # Open tasks on this company — what the team owes the prospect.
    open_tasks = []
    if company:
        tk_rows = (await db.execute(
            select(Task).where(
                Task.company_id == company.id,
                Task.completed == False,
            ).order_by(Task.due_date.asc().nulls_last(), Task.id).limit(6)
        )).scalars().all()
        open_tasks = [{
            "id": t.id,
            "description": t.description or "",
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "user_id": t.user_id,
        } for t in tk_rows]

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
        "lookup": {"email": email, "domain": domain, "linkedin": linkedin, "company_id": company_id},
        "contact": _serialize_contact(contact) if contact else None,
        "company": _serialize_company(company) if company else None,
        "sequence": sequence,
        "audit": audit,
        "activities": activities,
        "pinned_notes": pinned_notes,
        "other_contacts": other_contacts,
        "open_tasks": open_tasks,
        "recent_emails": recent_emails,
        "last_opened_at": last_opened_at,
        "last_clicked_at": last_clicked_at,
        "app_url": settings.public_url.rstrip("/"),
        "viewer": {"id": user.id, "first_name": user.first_name or "", "email": user.email},
        "team_emails": sorted(await missive_client.team_emails()),
        "missive_configured": missive_client.is_configured(),
        "missive_conversation_linked": bool(contact and contact.missive_conversation_id),
        # Available status values for the quick-change dropdown
        "status_options": [
            "new", "pursuing", "sequencing", "contacted",
            "replied", "qualified", "converted", "not_interested",
        ],
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
    db: AsyncSession = Depends(get_tenant_db),
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
    db: AsyncSession = Depends(get_tenant_db),
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
    db: AsyncSession = Depends(get_tenant_db),
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


class SetStatusRequest(BaseModel):
    company_id: int
    contact_id: Optional[int] = None
    new_status: str


@router.post("/sidebar/set-status")
async def sidebar_set_status(
    req: SetStatusRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Quick status change from the sidebar. Also fires the Missive tag
    sync (best-effort) when a conversation is linked."""
    valid = {"new", "pursuing", "sequencing", "contacted", "replied",
             "qualified", "converted", "not_interested"}
    if req.new_status not in valid:
        raise HTTPException(status_code=400, detail=f"invalid status — must be one of {sorted(valid)}")
    company = (await db.execute(
        select(Company).where(Company.id == req.company_id)
    )).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="company not found")
    old = company.status
    if old == req.new_status:
        return {"ok": True, "changed": False, "status": company.status}

    company.status = req.new_status
    db.add(Activity(
        company_id=company.id,
        contact_id=req.contact_id,
        user_id=user.id,
        activity_type="status_change",
        content=f"{user.first_name or user.email}: {old or '(unset)'} → {req.new_status}",
    ))
    await db.commit()

    # Best-effort Missive tag sync. We need a linked conversation_id on
    # one of the company's contacts; prefer the contact the BDR was
    # looking at, fall back to any contact that has a linked thread.
    label_applied = None
    try:
        from app.services import missive_client as _mc
        if _mc.is_configured():
            target_contact = None
            if req.contact_id:
                target_contact = (await db.execute(
                    select(Contact).where(Contact.id == req.contact_id)
                )).scalar_one_or_none()
            if not target_contact or not target_contact.missive_conversation_id:
                target_contact = (await db.execute(
                    select(Contact).where(
                        Contact.company_id == company.id,
                        Contact.missive_conversation_id.isnot(None),
                    ).order_by(desc(Contact.missive_conversation_seen_at)).limit(1)
                )).scalar_one_or_none()
            if target_contact and target_contact.missive_conversation_id:
                name = f"{(target_contact.first_name or '').strip()} {(target_contact.last_name or '').strip()}".strip()
                result = await _mc.sync_status_label(
                    conversation_id=target_contact.missive_conversation_id,
                    new_status=req.new_status,
                    contact_name=name or (target_contact.email or ""),
                    company_name=company.name or "",
                    actor=f"{user.first_name or user.email}",
                )
                if "_error" not in result:
                    label_applied = _mc.STATUS_TO_LABEL_NAME.get(req.new_status)
    except Exception:
        log.exception("sidebar/set-status: missive tag sync failed")

    return {"ok": True, "changed": True, "status": company.status, "label_applied": label_applied}


class CreateTaskRequest(BaseModel):
    company_id: int
    contact_id: Optional[int] = None
    description: str
    due_in_days: Optional[int] = None  # quick-set: 0=today, 1=tomorrow, 7=next week
    due_at_iso: Optional[str] = None   # explicit ISO 8601, takes precedence over due_in_days
    assignee_user_id: Optional[int] = None  # defaults to the caller


@router.post("/sidebar/create-task")
async def sidebar_create_task(
    req: CreateTaskRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Create a task scoped to the company (and optionally to a specific
    contact). Due date is set by `due_at_iso` if provided, else
    derived from `due_in_days` (0..30); else left null. Defaults
    assignee to the caller."""
    desc = (req.description or "").strip()
    if not desc:
        raise HTTPException(status_code=400, detail="description is required")
    if len(desc) > 500:
        desc = desc[:500]

    # Resolve due date
    due_at = None
    if req.due_at_iso:
        try:
            due_at = datetime.fromisoformat(req.due_at_iso.replace("Z", "+00:00"))
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
        except Exception:
            raise HTTPException(status_code=400, detail="due_at_iso must be ISO 8601")
    elif req.due_in_days is not None:
        days = max(0, min(int(req.due_in_days), 30))
        # Use start-of-day in UTC; close-enough for a CRM task
        base = datetime.now(timezone.utc).replace(hour=17, minute=0, second=0, microsecond=0)
        due_at = base + timedelta(days=days)

    assignee_id = req.assignee_user_id or user.id

    # Validate the assignee actually exists (and is in our user table)
    assignee = (await db.execute(select(User).where(User.id == assignee_id))).scalar_one_or_none()
    if not assignee:
        raise HTTPException(status_code=400, detail="assignee not found")

    task = Task(
        company_id=req.company_id,
        contact_id=req.contact_id,
        user_id=assignee.id,
        description=desc,
        due_date=due_at,
        completed=False,
    )
    db.add(task)
    db.add(Activity(
        company_id=req.company_id,
        contact_id=req.contact_id,
        user_id=user.id,
        activity_type="task_created",
        content=f"[Sidebar] {user.first_name or user.email} created task for {assignee.first_name or assignee.email}: {desc}"
                + (f" (due {due_at.date().isoformat()})" if due_at else ""),
    ))
    await db.commit()
    await db.refresh(task)
    return {
        "ok": True,
        "task": {
            "id": task.id,
            "description": task.description,
            "due_date": task.due_date.isoformat() if task.due_date else None,
            "assignee_id": task.user_id,
            "assignee_name": assignee.first_name or assignee.email,
        },
    }


class CompleteTaskRequest(BaseModel):
    task_id: int


@router.post("/sidebar/complete-task")
async def sidebar_complete_task(
    req: CompleteTaskRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Mark a task complete from the sidebar."""
    task = (await db.execute(select(Task).where(Task.id == req.task_id))).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    now = datetime.now(timezone.utc)
    task.completed = True
    task.completed_at = now
    db.add(Activity(
        company_id=task.company_id,
        contact_id=task.contact_id,
        user_id=user.id,
        activity_type="task_completed",
        content=f"{user.first_name or user.email} completed: {task.description}",
    ))

    # Mirror crm_routes.complete_task: propagate completion to the linked
    # sequence step so the Stalled tab clears.
    step = (await db.execute(
        select(GeneratedEmail).where(GeneratedEmail.task_id == task.id)
    )).scalar_one_or_none()
    if step and not step.is_sent:
        step.is_sent = True
        step.sent_at = now

    await db.commit()
    return {"ok": True, "task_id": task.id}


class SendIMessageRequest(BaseModel):
    contact_id: int
    body: str


@router.post("/sidebar/send-imessage")
async def sidebar_send_imessage(
    req: SendIMessageRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Send a quick iMessage to the contact's phone — uses the Blooio
    sender that powers automated iMessage steps. No-ops cleanly if
    phone is missing, opted-out, or not a mobile line."""
    contact = (await db.execute(select(Contact).where(Contact.id == req.contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="contact not found")
    body = (req.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="body required")
    if not contact.phone:
        return {"ok": False, "reason": "no phone number on contact"}
    if getattr(contact, "do_not_text", False):
        return {"ok": False, "reason": "contact has opted out of SMS/iMessage"}

    try:
        from app.runtime_config import get_blooio_api_key
        from app.services.blooio_messaging import send_message as blooio_send
        api_key = await get_blooio_api_key(db)
        if not api_key:
            return {"ok": False, "reason": "iMessage service not configured"}
        result = await blooio_send(api_key=api_key, to_phone=contact.phone, text=body)
        if not getattr(result, "ok", False):
            return {"ok": False, "reason": getattr(result, "error", "send failed")}
        db.add(Activity(
            company_id=contact.company_id,
            contact_id=contact.id,
            user_id=user.id,
            activity_type="imessage_sent",
            content=f"[Sidebar] iMessage → {contact.phone}: {body[:200]}",
        ))
        await db.commit()
        return {"ok": True, "message_id": getattr(result, "message_id", None)}
    except Exception as e:
        log.exception("sidebar/send-imessage failed")
        return {"ok": False, "reason": str(e)}


class SendNextStepRequest(BaseModel):
    contact_id: int


@router.post("/sidebar/send-next-step")
async def sidebar_send_next_step(
    req: SendNextStepRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Pull the soonest-scheduled pending action for THIS contact and
    bump its scheduled_at to NOW so the next minute's dispatcher cron
    tick claims it. We do NOT run a dispatcher tick inline — that would
    sweep every tenant's due actions on this request and fire them
    under the wrong tenant scope. The BDR's action goes out within ≤60s
    when the cron runs.

    Tenant scoping: the action must belong to the request's tenant (we
    verify by joining contacts; an out-of-tenant contact_id returns the
    same 'no pending action' as one that genuinely has no scheduled
    work)."""
    from sqlalchemy import text as _sa_text

    # Tenant-scoped lookup. The contact-join + e.tenant_id = c.tenant_id
    # guards against accidentally bumping another tenant's action.
    row = (await db.execute(_sa_text("""
        SELECT a.id FROM actions a
        JOIN contacts c ON c.id = a.contact_id
        WHERE a.contact_id = :c
          AND a.status = 'scheduled'
          AND a.tenant_id = c.tenant_id
        ORDER BY a.scheduled_at ASC, a.id ASC
        LIMIT 1
    """), {"c": req.contact_id})).first()
    if row is None:
        return {"fired": False, "reason": "no pending action"}
    action_id = int(row[0])

    # Bump scheduled_at to NOW. The next cron tick (≤60s) will claim it.
    await db.execute(_sa_text("""
        UPDATE actions
        SET scheduled_at = NOW(),
            sent_by_user_id = COALESCE(sent_by_user_id, :uid)
        WHERE id = :aid
    """), {"aid": action_id, "uid": user.id})
    await db.commit()

    return {
        "fired": False,
        "queued_for_next_tick": True,
        "action_id": action_id,
        "reason": "bumped to NOW; dispatcher cron will send within 60s",
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
