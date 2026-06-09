"""
Company-level routes: list, detail, enrichment, and the prospector pursue flow.

The pursue flow is the single most important integration point:
when a Company is pursued, we auto-create Contacts (from Apollo/Hunter),
auto-create a Deal in the pipeline, and generate the email sequence —
so the team sees queued messages BEFORE they send.
"""
from __future__ import annotations
import json
import secrets
from typing import Optional
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_
from pydantic import BaseModel

from app.tenancy import get_tenant_db
from app.models import User, Company, Contact, Deal, GeneratedEmail, Activity, Task, Tag, company_tags, CustomFieldDefinition
from app.auth import get_current_user, mint_recording_token
from app.services import pipeline_config as _pipeline_cfg_pursue
from app.services.website_intel import analyze_website, analysis_to_dict
from app.services.email_generator import generate_cold_email, generate_follow_up, generate_linkedin_message
from app.services.hunter_enrichment import search_domain as hunter_search
from app.services.netrows_enrichment import (
    find_decision_makers as netrows_find_decision_makers,
    google_maps_reviews as netrows_maps_reviews,
    reverse_email_lookup as netrows_reverse_lookup,
    enrich_company_by_domain as netrows_company_enrich,
)
from app.services.local_seo_intel import analyze_local_seo, local_seo_to_dict
from app.config import settings
from app.runtime_config import get_netrows_api_key

router = APIRouter(prefix="/api/companies", tags=["companies"])


# ============================================================
# List + detail
# ============================================================

@router.get("/")
async def list_companies(
    search_id: Optional[int] = None,
    status: Optional[str] = None,
    lifecycle: Optional[str] = None,
    enriched_only: bool = False,
    min_reviews: Optional[int] = None,
    max_reviews: Optional[int] = None,
    min_rating: Optional[float] = None,
    has_website: Optional[bool] = None,
    rep_id: Optional[int] = None,
    has_sequence: Optional[bool] = None,   # True = email_generated, False = not yet
    tag_id: Optional[int] = None,          # Filter to companies with this tag
    business_type_contains: Optional[str] = None,  # Substring match on business_type
    snoozed: Optional[bool] = None,  # True = only snoozed; False = only active (not snoozed); None = no filter
    sort_by: str = "reviews",
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    from app.scoping import scope_companies
    query = scope_companies(select(Company), user, rep_id)
    if search_id:
        query = query.where(Company.search_id == search_id)
    if status:
        query = query.where(Company.status == status)
    if lifecycle == "active":
        query = query.where(Company.status != "new")
    elif lifecycle == "new":
        query = query.where(Company.status == "new")
    if enriched_only:
        query = query.where(Company.enriched == True)
    if min_reviews:
        query = query.where(Company.review_count >= min_reviews)
    if max_reviews:
        query = query.where(Company.review_count <= max_reviews)
    if min_rating:
        query = query.where(Company.rating >= min_rating)
    if has_website is True:
        query = query.where(Company.website.isnot(None), Company.website != "")
    if has_sequence is True:
        query = query.where(Company.email_generated == True)
    elif has_sequence is False:
        query = query.where(Company.email_generated == False)
    if tag_id:
        query = query.where(
            Company.id.in_(select(company_tags.c.company_id).where(company_tags.c.tag_id == tag_id))
        )
    if business_type_contains:
        query = query.where(Company.business_type.ilike(f"%{business_type_contains}%"))
    if snoozed is True:
        # Only snoozed companies (resume date is set and still in the future).
        # Past-resume rows are conceptually awake even before the engine clears
        # the field, so we filter them out.
        query = query.where(
            Company.sequence_resume_at.is_not(None),
            Company.sequence_resume_at > datetime.now(timezone.utc),
        )
    elif snoozed is False:
        query = query.where(
            (Company.sequence_resume_at.is_(None)) |
            (Company.sequence_resume_at <= datetime.now(timezone.utc))
        )

    if sort_by == "reviews":
        query = query.order_by(Company.review_count.desc().nullslast())
    elif sort_by == "rating":
        query = query.order_by(Company.rating.desc().nullslast())
    elif sort_by == "name":
        query = query.order_by(Company.name.asc())
    elif sort_by == "lead_score":
        query = query.order_by(Company.lead_score.desc().nullslast())
    elif sort_by == "activity":
        query = query.order_by(Company.updated_at.desc().nullslast())
    else:
        query = query.order_by(Company.created_at.desc())

    result = await db.execute(query)
    companies = result.scalars().all()
    company_ids = [c.id for c in companies]

    # Prefetch BDR names — single query instead of N+1.
    assigned_ids = {c.assigned_to for c in companies if c.assigned_to}
    user_name_map: dict[int, str] = {}
    if assigned_ids:
        rows = (await db.execute(
            select(User.id, User.first_name, User.last_name, User.email)
            .where(User.id.in_(assigned_ids))
        )).all()
        for uid, fn, ln, email in rows:
            user_name_map[uid] = (f"{fn or ''} {ln or ''}".strip() or email)

    # Prefetch tags for all returned companies — single join query.
    tags_map: dict[int, list] = {cid: [] for cid in company_ids}
    if company_ids:
        tag_rows = (await db.execute(
            select(company_tags.c.company_id, Tag.id, Tag.name, Tag.color)
            .join(Tag, Tag.id == company_tags.c.tag_id)
            .where(company_tags.c.company_id.in_(company_ids))
        )).all()
        for cid, tid, tname, tcolor in tag_rows:
            tags_map[cid].append({"id": tid, "name": tname, "color": tcolor or "#888"})

    # Prefetch next unsent sequence step per company — single aggregate query.
    # Returns the lowest sequence_order among unsent emails so the UI can
    # show "Step 2" meaning the company is waiting on step 2.
    seq_step_map: dict[int, int | None] = {}
    if company_ids:
        step_rows = (await db.execute(
            select(GeneratedEmail.company_id, func.min(GeneratedEmail.sequence_order))
            .where(
                GeneratedEmail.company_id.in_(company_ids),
                GeneratedEmail.is_sent == False,
            )
            .group_by(GeneratedEmail.company_id)
        )).all()
        for cid, min_step in step_rows:
            seq_step_map[cid] = min_step

    # Prefetch contact counts — single aggregate query.
    contact_count_map: dict[int, int] = {}
    if company_ids:
        count_rows = (await db.execute(
            select(Contact.company_id, func.count(Contact.id))
            .where(Contact.company_id.in_(company_ids))
            .group_by(Contact.company_id)
        )).all()
        for cid, cnt in count_rows:
            contact_count_map[cid] = cnt

    return [
        _company_summary(
            c,
            assigned_name=user_name_map.get(c.assigned_to),
            tags=tags_map.get(c.id, []),
            sequence_next_step=seq_step_map.get(c.id),
            contact_count=contact_count_map.get(c.id, 0),
        )
        for c in companies
    ]


@router.get("/stalled-sequences")
async def get_stalled_sequences(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Return all sequence steps that are stalled, grouped by company.

    Stall categories:
      critical     — auto_execute step past due >2 h (engine missed it)
      needs_action — manual step (call/linkedin) past due (BDR hasn't acted)
      paused       — paused_at set on an active sequence

    Companies in terminal states (not_interested, qualified, converted) are
    excluded entirely: any paused steps on those were correctly halted by
    the disqualify / win flows — not stalled. Including them inflated this
    view by 60%+ and diluted the signal (found 2026-06-02: 615 of 1,008
    paused steps belonged to already-disqualified companies).
    """
    from app.scoping import scope_companies
    now = datetime.now(timezone.utc)
    grace = timedelta(hours=2)

    # Scope to companies this user can see, AND drop companies whose status
    # already represents a final outcome.
    scoped_ids_q = scope_companies(select(Company.id), user, None)
    TERMINAL_STATUSES = ("not_interested", "qualified", "converted")

    rows = (await db.execute(
        select(GeneratedEmail, Contact, Company)
        .join(Contact, GeneratedEmail.contact_id == Contact.id)
        .join(Company, GeneratedEmail.company_id == Company.id)
        .where(
            GeneratedEmail.is_sent == False,
            GeneratedEmail.skipped_at.is_(None),
            GeneratedEmail.company_id.in_(scoped_ids_q),
            Company.status.notin_(TERMINAL_STATUSES),
            or_(
                # Critical: auto step engine missed
                and_(
                    GeneratedEmail.auto_execute == True,
                    GeneratedEmail.paused_at.is_(None),
                    GeneratedEmail.scheduled_send_at != None,
                    GeneratedEmail.scheduled_send_at < now - grace,
                ),
                # Needs action: manual step BDR hasn't completed
                and_(
                    GeneratedEmail.auto_execute == False,
                    GeneratedEmail.paused_at.is_(None),
                    GeneratedEmail.scheduled_send_at != None,
                    GeneratedEmail.scheduled_send_at < now,
                ),
                # Paused: someone halted this step
                and_(
                    GeneratedEmail.paused_at.is_not(None),
                    GeneratedEmail.is_sent == False,
                ),
            ),
        )
        .order_by(Company.name, GeneratedEmail.scheduled_send_at)
    )).all()

    # POST-CUTOVER: also pull engine actions that are stalled. The widget
    # was completely blind to engine state until now — engine actions
    # overdue >2h, manual engine steps awaiting BDR, and paused engine
    # actions all invisible. With most contacts now engine-enrolled, the
    # widget was reading consistently "all healthy" while the engine
    # could have been dead behind the scenes.
    from sqlalchemy import text as _sa_text
    auto_channels = ("email", "sms")  # engine channels that fire automatically
    engine_rows = (await db.execute(_sa_text("""
        SELECT
          a.id, a.scheduled_at, a.status,
          ct.code AS channel_code,
          a.subject,
          c.id AS contact_id, c.first_name, c.last_name, c.email, c.phone,
          co.id AS company_id, co.name AS company_name, co.status AS company_status,
          (SELECT MAX(updated_at) FROM actions WHERE id = a.id) AS paused_at_proxy
        FROM actions a
        JOIN channel_types ct ON ct.id = a.channel_id
        JOIN contacts c ON c.id = a.contact_id
        JOIN companies co ON co.id = c.company_id
        JOIN engagements e ON e.id = a.engagement_id
        WHERE co.status NOT IN ('not_interested','qualified','converted')
          AND e.status = 'active'
          AND (
            (a.status = 'scheduled' AND ct.code IN :auto AND a.scheduled_at < :critical_cutoff)
            OR (a.status = 'scheduled' AND ct.code NOT IN :auto AND a.scheduled_at < :now)
            OR (a.status = 'paused')
          )
        ORDER BY co.name, a.scheduled_at
    """).bindparams(
        # asyncpg requires tuples for IN with bound params
    ), {
        "auto": tuple(auto_channels),
        "critical_cutoff": now - grace,
        "now": now,
    })).fetchall()

    # Append engine rows to the grouping using the same dict shape so
    # the rendering loop below treats them uniformly.
    engine_grouped: list = []  # list of (synthetic_ge_dict, contact_proxy, company_proxy)

    class _Proxy:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    for r in engine_rows:
        is_auto = r.channel_code in auto_channels
        if r.status == "paused":
            severity = "paused"
        elif is_auto:
            severity = "critical"
        else:
            severity = "needs_action"
        synth_ge = _Proxy(
            id=int(r.id),
            step_type={"email":"email","sms":"imessage","call_task":"call",
                       "linkedin":"linkedin","manual":"manual"}.get(r.channel_code, r.channel_code),
            sequence_label="engine",
            email_type=None,
            sequence_order=None,
            paused_at=(r.paused_at_proxy if r.status == "paused" else None),
            scheduled_send_at=r.scheduled_at,
            auto_execute=is_auto,
            company_id=int(r.company_id),
            engine_marker="engagement_engine",
        )
        synth_contact = _Proxy(
            id=int(r.contact_id),
            first_name=r.first_name, last_name=r.last_name,
            email=r.email, phone=r.phone,
        )
        synth_company = _Proxy(
            id=int(r.company_id), name=r.company_name, status=r.company_status,
        )
        engine_grouped.append((synth_ge, synth_contact, synth_company))

    rows = list(rows) + engine_grouped

    # Group by company
    by_company: dict[int, dict] = {}
    for ge, contact, company in rows:
        if company.id not in by_company:
            by_company[company.id] = {
                "company_id": company.id,
                "company_name": company.name,
                "company_status": company.status,
                "stalls": [],
            }
        # Determine severity + human reason
        if ge.paused_at:
            severity = "paused"
            overdue_hours = round((now - ge.paused_at).total_seconds() / 3600)
            reason = f"Paused {overdue_hours}h ago"
        elif ge.auto_execute:
            overdue_hours = round((now - ge.scheduled_send_at).total_seconds() / 3600)
            overdue_days = overdue_hours // 24
            severity = "critical"
            reason = (
                f"Auto-send overdue by {overdue_days}d {overdue_hours % 24}h"
                if overdue_days else f"Auto-send overdue by {overdue_hours}h"
            )
        else:
            overdue_hours = round((now - ge.scheduled_send_at).total_seconds() / 3600)
            overdue_days = overdue_hours // 24
            severity = "needs_action"
            reason = (
                f"Waiting {overdue_days}d {overdue_hours % 24}h for BDR action"
                if overdue_days else f"Waiting {overdue_hours}h for BDR action"
            )

        by_company[company.id]["stalls"].append({
            "step_id": ge.id,
            "step_type": ge.step_type or "email",
            "label": ge.sequence_label or ge.email_type or "",
            "sequence_order": ge.sequence_order,
            "contact_id": contact.id,
            "contact_name": f"{contact.first_name or ''} {contact.last_name or ''}".strip() or contact.email or "(no name)",
            "contact_email": contact.email,
            "contact_phone": contact.phone,
            "severity": severity,
            "reason": reason,
            "overdue_hours": overdue_hours if ge.paused_at is None else None,
            "scheduled_send_at": ge.scheduled_send_at.isoformat() if ge.scheduled_send_at else None,
            "paused_at": ge.paused_at.isoformat() if ge.paused_at else None,
            "auto_execute": ge.auto_execute,
        })

    # Compute per-company top severity for sorting
    sev_order = {"critical": 0, "needs_action": 1, "paused": 2}
    result = list(by_company.values())
    for item in result:
        item["top_severity"] = min(
            (sev_order.get(s["severity"], 9) for s in item["stalls"]),
            default=9,
        )
    result.sort(key=lambda x: (x["top_severity"], x["company_name"]))
    return result


@router.post("/{company_id}/unstall-sequences")
async def unstall_sequences(
    company_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Re-anchor all overdue auto-execute steps for this company to fire ASAP.
    Manual steps (call/linkedin) are left alone — BDR still needs to act."""
    from app.scoping import scope_companies
    company = (await db.execute(
        scope_companies(select(Company).where(Company.id == company_id), user, None)
    )).scalar_one_or_none()
    if not company:
        raise HTTPException(404, "Company not found")

    now = datetime.now(timezone.utc)
    grace = timedelta(hours=2)

    stalled = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.company_id == company_id,
            GeneratedEmail.auto_execute == True,
            GeneratedEmail.is_sent == False,
            GeneratedEmail.paused_at.is_(None),
            GeneratedEmail.skipped_at.is_(None),
            GeneratedEmail.scheduled_send_at != None,
            GeneratedEmail.scheduled_send_at < now - grace,
        ).order_by(GeneratedEmail.sequence_order)
    )).scalars().all()

    if not stalled:
        return {"reanchored": 0, "message": "No overdue auto-execute steps found"}

    # Re-anchor: spread from now, maintaining relative offsets
    base = min(s.send_delay_days or 0 for s in stalled)
    for i, step in enumerate(stalled):
        offset = max((step.send_delay_days or 0) - base, 0)
        step.scheduled_send_at = now + timedelta(days=offset, minutes=i * 2)

    # Snap to send window
    try:
        from app.services.send_window import snap_pending_steps_to_window
        contact_ids = {s.contact_id for s in stalled if s.contact_id}
        for cid in contact_ids:
            await snap_pending_steps_to_window(db, contact_id=cid)
    except Exception:
        pass

    await db.commit()
    return {"reanchored": len(stalled), "message": f"Re-anchored {len(stalled)} step(s) to send ASAP"}


@router.get("/paused-forgotten")
async def get_paused_forgotten(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Companies with paused sequences that have no auto-pause trigger —
    a BDR manually paused them and never came back to either resume or
    disqualify. These are decision-debt: each one needs a yes/no from
    the assigned rep.

    A 'forgotten pause' is one where:
      - The company is still in 'sequencing' (not disqualified / won)
      - At least one unsent step has paused_at set
      - The contact has NO reply / bounce / unsubscribe / archive trigger
        that would explain why the auto-pause logic halted the sequence

    Scoped to the current user's companies (admins see all). One row per
    company, sorted by oldest pause first so the most-forgotten surface.
    """
    from app.scoping import scope_companies
    scoped_ids_q = scope_companies(select(Company.id), user, None)

    # Pull every paused step on an active company. We classify in Python
    # (rather than a complex SQL EXISTS chain) so it stays readable.
    rows = (await db.execute(
        select(GeneratedEmail, Contact, Company)
        .join(Contact, GeneratedEmail.contact_id == Contact.id)
        .join(Company, GeneratedEmail.company_id == Company.id)
        .where(
            GeneratedEmail.is_sent == False,
            GeneratedEmail.skipped_at.is_(None),
            GeneratedEmail.paused_at.is_not(None),
            GeneratedEmail.company_id.in_(scoped_ids_q),
            Company.status == "sequencing",
        )
        .order_by(GeneratedEmail.paused_at)
    )).all()
    if not rows:
        return []

    # Bulk-load reply Activities for these contacts (one query, not N)
    contact_ids = list({contact.id for _ge, contact, _co in rows})
    replied_set: set[int] = set()
    if contact_ids:
        reply_rows = (await db.execute(
            select(Activity.contact_id).where(
                Activity.contact_id.in_(contact_ids),
                Activity.activity_type.in_(
                    ["email_replied", "email_auto_response",
                     "imessage_received", "reply_received"]
                ),
            ).distinct()
        )).all()
        replied_set = {r[0] for r in reply_rows}

    # Bulk-load BDR names for assigned_to
    bdr_ids = list({co.assigned_to for _ge, _ct, co in rows if co.assigned_to})
    bdr_map: dict[int, str] = {}
    if bdr_ids:
        for uid, fn, ln, email in (await db.execute(
            select(User.id, User.first_name, User.last_name, User.email)
            .where(User.id.in_(bdr_ids))
        )).all():
            bdr_map[uid] = (f"{fn or ''} {ln or ''}".strip() or email)

    # Group by company; skip steps whose pause has an explainable trigger
    by_company: dict[int, dict] = {}
    for ge, contact, company in rows:
        # Skip steps where the pause has an obvious auto-pause trigger.
        if contact.id in replied_set: continue
        if (contact.email_status or "") == "bounced": continue
        if contact.unsubscribed_at: continue
        if contact.is_archived: continue
        if ge.step_type == "imessage" and contact.do_not_text: continue

        d = by_company.setdefault(company.id, {
            "company_id": company.id,
            "company_name": company.name,
            "company_city": company.city,
            "company_state": company.state,
            "company_status": company.status,
            "assigned_to": company.assigned_to,
            "assigned_name": bdr_map.get(company.assigned_to) if company.assigned_to else None,
            "paused_step_count": 0,
            "oldest_paused_at": None,
            "primary_contact_name": None,
        })
        d["paused_step_count"] += 1
        paused_iso = ge.paused_at.isoformat() if ge.paused_at else None
        if paused_iso and (d["oldest_paused_at"] is None or paused_iso < d["oldest_paused_at"]):
            d["oldest_paused_at"] = paused_iso
        # Record the first contact name encountered (the rows are ordered
        # by paused_at, so this is stable enough for a list view).
        if not d["primary_contact_name"]:
            d["primary_contact_name"] = (
                f"{contact.first_name or ''} {contact.last_name or ''}".strip()
                or contact.email or "(no name)"
            )

    # Return oldest-pause-first so the longest-ignored surface
    result = list(by_company.values())
    result.sort(key=lambda x: x["oldest_paused_at"] or "")
    return result


# ============================================================
# Disqualify / restore (BDR marks unqualified → admin reviews)
# ============================================================

DISQUALIFY_REASONS = [
    "Not a fit for our services",
    "Already has a vendor / locked in contract",
    "Too small — not enough budget",
    "Too large — not our target",
    "Couldn't reach decision-maker",
    "No budget right now",
    "Not interested — do not contact",
    "Bad contact info — can't reach them",
    "Out of service area",
    "Other",
]


class DisqualifyRequest(BaseModel):
    reason: str
    notes: Optional[str] = None


@router.post("/{company_id}/disqualify")
async def disqualify_company(
    company_id: int,
    req: DisqualifyRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Mark a company as unqualified, pause all active sequence steps,
    and log an activity so admins can review the decision."""
    from app.scoping import scope_companies
    company = (await db.execute(
        scope_companies(select(Company).where(Company.id == company_id), user, None)
    )).scalar_one_or_none()
    if not company:
        raise HTTPException(404, "Company not found")

    now = datetime.now(timezone.utc)
    old_status = company.status

    # Update company
    company.status = "not_interested"
    full_reason = req.reason if not req.notes else f"{req.reason} — {req.notes}"
    company.lost_reason = full_reason

    # Disqualify wins over snooze. Clear any active snooze fields so the
    # engine's wake-up loop doesn't try to regenerate a sequence for a
    # company we've just marked terminal.
    had_snooze = company.sequence_resume_at is not None
    if had_snooze:
        company.sequence_resume_at = None
        company.sequence_snoozed_at = None
        company.sequence_snooze_reason = None
        company.sequence_snoozed_by_user_id = None
        company.sequence_snooze_days = None

    # Pause every unsent, unpaused sequence step for this company
    active_steps = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.company_id == company_id,
            GeneratedEmail.is_sent == False,
            GeneratedEmail.paused_at.is_(None),
            GeneratedEmail.skipped_at.is_(None),
        )
    )).scalars().all()
    for step in active_steps:
        step.paused_at = now

    # Log activity — 'disqualified' type so admin dashboard can filter it
    snooze_note = " (snooze cleared)" if had_snooze else ""
    db.add(Activity(
        company_id=company_id,
        user_id=user.id,
        activity_type="disqualified",
        content=f"Marked as unqualified: {full_reason} (was: {old_status}){snooze_note}",
    ))

    await db.commit()
    return {
        "company_id": company_id,
        "status": "not_interested",
        "lost_reason": full_reason,
        "steps_paused": len(active_steps),
    }


class SnoozeCompanyRequest(BaseModel):
    """Pause this company's sequence until a future date. Exactly one of
    {days, until_date} must be set."""
    days: Optional[int] = None       # 1..365
    until_date: Optional[str] = None # ISO 8601 date (YYYY-MM-DD)
    reason: Optional[str] = None


@router.post("/{company_id}/snooze")
async def snooze_company(
    company_id: int,
    req: SnoozeCompanyRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Snooze the entire outbound sequence for a company until a chosen
    date. The engine suppresses every outbound step for this company while
    snoozed. On wake, the engine regenerates a fresh tailored sequence
    whose first email references the agreed timeframe."""
    from app.scoping import scope_companies
    company = (await db.execute(
        scope_companies(select(Company).where(Company.id == company_id), user, None)
    )).scalar_one_or_none()
    if not company:
        raise HTTPException(404, "Company not found")

    # Disqualified companies can't be snoozed — restore first.
    if (company.status or "") == "not_interested":
        raise HTTPException(400, "Cannot snooze a disqualified company. Restore first.")

    # Resolve wake time + snooze_days from whichever input the BDR gave.
    now = datetime.now(timezone.utc)
    days_chosen: Optional[int] = None
    if req.days is not None and req.until_date:
        raise HTTPException(400, "Provide either days OR until_date, not both.")
    if req.days is not None:
        if req.days < 1 or req.days > 365:
            raise HTTPException(400, "days must be between 1 and 365")
        days_chosen = req.days
        resume_at = now + timedelta(days=req.days)
    elif req.until_date:
        try:
            d = datetime.fromisoformat(req.until_date)
        except ValueError:
            raise HTTPException(400, "until_date must be ISO 8601 (YYYY-MM-DD)")
        # Treat date-only as end-of-day local-equivalent UTC midnight
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        if d <= now:
            raise HTTPException(400, "until_date must be in the future")
        resume_at = d
        days_chosen = max(1, (d - now).days)
    else:
        raise HTTPException(400, "Provide either days or until_date")

    was_snoozed = company.sequence_resume_at is not None
    prior_resume = company.sequence_resume_at

    company.sequence_resume_at = resume_at
    company.sequence_snoozed_at = now
    company.sequence_snooze_reason = (req.reason or "").strip() or None
    company.sequence_snoozed_by_user_id = user.id
    company.sequence_snooze_days = days_chosen

    # Don't mutate generated_emails — the legacy dispatch gate in
    # sequence_engine.process_pending_steps blocks them while
    # sequence_resume_at > now, and the wake handler regenerates a
    # fresh sequence then.
    # POST-CUTOVER: the new engagement engine has NO equivalent gate —
    # it dispatches on action.status='scheduled' + scheduled_at <= now,
    # ignoring company.sequence_resume_at entirely. So an engine-enrolled
    # snoozed company would keep sending. We must explicitly pause the
    # engagement so its action.status flips to 'paused' across the board.
    from sqlalchemy import text as _sa_text
    legacy_pending = (await db.execute(
        select(func.count(GeneratedEmail.id)).where(
            GeneratedEmail.company_id == company_id,
            GeneratedEmail.is_sent == False,
            GeneratedEmail.skipped_at.is_(None),
            GeneratedEmail.paused_at.is_(None),
        )
    )).scalar() or 0
    engine_pending = (await db.execute(_sa_text("""
        SELECT COUNT(*) FROM actions a
        JOIN engagements e ON e.id = a.engagement_id
        WHERE e.company_id = :co AND a.status = 'scheduled'
    """), {"co": company_id})).scalar() or 0
    pending_count = int(legacy_pending) + int(engine_pending)

    # Pause every engagement engine engagement at this company so their
    # actions stop firing during the snooze window. resume happens on
    # unsnooze OR via the wake cron (_wake_snoozed_deals now resumes too).
    engine_paused_total = 0
    try:
        from app.engagement_engine.lifecycle import pause_engagement
        from app.models import Contact as _Contact
        contacts_at_co = (await db.execute(
            select(_Contact).where(_Contact.company_id == company_id)
        )).scalars().all()
        for c in contacts_at_co:
            try:
                engine_paused_total += await pause_engagement(
                    db, c.id,
                    reason=f"company snoozed via UI until {resume_at.isoformat()}",
                )
            except Exception:
                pass
    except Exception as _pe:
        pass  # snooze itself must not fail because of engine bookkeeping

    audit_msg = (
        f"Snooze extended from {prior_resume.strftime('%b %d, %Y') if prior_resume else '—'} "
        f"to {resume_at.strftime('%b %d, %Y')}"
        if was_snoozed
        else f"Snoozed until {resume_at.strftime('%b %d, %Y')}"
    )
    if req.reason:
        audit_msg += f" — {req.reason}"
    db.add(Activity(
        company_id=company_id,
        user_id=user.id,
        activity_type="sequence_snoozed",
        content=audit_msg,
    ))
    await db.commit()
    return {
        "ok": True,
        "company_id": company_id,
        "sequence_resume_at": resume_at.isoformat(),
        "sequence_snooze_days": days_chosen,
        "sequence_snooze_reason": company.sequence_snooze_reason,
        "paused_step_count": int(pending_count),
    }


@router.post("/{company_id}/unsnooze")
async def unsnooze_company(
    company_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Manually wake a snoozed company before its scheduled wake date.
    Re-enrolls every contact whose engagement is terminal (declined) or
    absent, via the engagement engine's lifecycle module."""
    from app.scoping import scope_companies
    from app.engagement_engine.lifecycle import wake_engagement_for_company
    company = (await db.execute(
        scope_companies(select(Company).where(Company.id == company_id), user, None)
    )).scalar_one_or_none()
    if not company:
        raise HTTPException(404, "Company not found")
    if company.sequence_resume_at is None:
        return {"ok": True, "company_id": company_id, "already_awake": True, "steps_created": 0}

    woke_early = company.sequence_resume_at > datetime.now(timezone.utc)
    # Clear legacy snooze flags so downstream queries see the company awake.
    company.sequence_resume_at = None
    company.sequence_snoozed_at = None
    company.sequence_snooze_days = None
    company.sequence_snooze_reason = None
    n = await wake_engagement_for_company(db, company, initiated_by="bdr_unsnooze")

    # POST-CUTOVER: also resume the engagement-engine engagements so
    # the paused actions go back to 'scheduled' and start firing again.
    engine_resumed_total = 0
    try:
        from app.engagement_engine.lifecycle import resume_engagement
        from app.models import Contact as _Contact
        contacts_at_co = (await db.execute(
            select(_Contact).where(_Contact.company_id == company_id)
        )).scalars().all()
        for c in contacts_at_co:
            try:
                engine_resumed_total += await resume_engagement(db, c.id)
            except Exception:
                pass
    except Exception:
        pass

    db.add(Activity(
        company_id=company_id,
        user_id=user.id,
        activity_type="sequence_unsnoozed",
        content=f"Manually woken{'(early)' if woke_early else ''} — regenerated {n} step(s), resumed {engine_resumed_total} engine action(s)",
    ))
    await db.commit()
    return {
        "ok": True, "company_id": company_id,
        "woke_early": woke_early,
        "steps_created": n,
        "engine_actions_resumed": engine_resumed_total,
    }


@router.post("/{company_id}/restore-disqualify")
async def restore_disqualified_company(
    company_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Restore a disqualified company back to active pursuit. ALSO un-pauses
    the future sequence steps the disqualify action paused — so the BDR
    doesn't have to manually resume them too.

    Opened to all authenticated users 2026-06-04 per team request — BDRs
    sometimes change their mind about a disqualification and shouldn't
    need to chase an admin for approval. The Activity log still captures
    who restored, so misuse is recoverable via admin review of
    'status_change' activities.
    """
    company = (await db.execute(
        select(Company).where(Company.id == company_id)
    )).scalar_one_or_none()
    if not company:
        raise HTTPException(404, "Company not found")

    if company.status != "not_interested":
        raise HTTPException(
            400, f"Company is not disqualified (current status: {company.status!r})"
        )

    old_reason = company.lost_reason
    company.status = "pursuing"
    company.lost_reason = None

    # Engagement engine: re-enroll every contact whose engagement is
    # terminal (declined) or absent. wake_engagement_for_company is
    # idempotent for contacts whose engagement is already active.
    from app.engagement_engine.lifecycle import wake_engagement_for_company
    resumed_count = await wake_engagement_for_company(
        db, company, initiated_by="bdr_restore_disqualify",
    )

    db.add(Activity(
        company_id=company_id,
        user_id=user.id,
        activity_type="status_change",
        content=(
            f"Restored from disqualified (reason was: {old_reason or 'not recorded'}). "
            f"Re-enrolled {resumed_count} contact(s) in the engagement engine."
        ),
    ))

    await db.commit()
    return {
        "company_id": company_id,
        "status": "pursuing",
        "resumed_steps": resumed_count,
    }


@router.get("/pending-review")
async def get_pending_review(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Admin-only: companies disqualified in the last 30 days, with the
    reason and the BDR who logged the decision."""
    if user.role not in ("admin", "super_admin"):
        raise HTTPException(403, "Admin only")

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    # Most recent 'disqualified' activity per company
    rows = (await db.execute(
        select(Activity, Company, User)
        .join(Company, Activity.company_id == Company.id)
        .outerjoin(User, Activity.user_id == User.id)
        .where(
            Activity.activity_type == "disqualified",
            Activity.created_at >= cutoff,
            Company.status == "not_interested",
        )
        .order_by(Activity.created_at.desc())
    )).all()

    # Dedupe: one row per company (keep most recent)
    seen: set[int] = set()
    result = []
    for act, company, bdru in rows:
        if company.id in seen:
            continue
        seen.add(company.id)
        bdr_name = None
        if bdru:
            bdr_name = f"{bdru.first_name or ''} {bdru.last_name or ''}".strip() or bdru.email
        result.append({
            "company_id": company.id,
            "company_name": company.name,
            "company_city": company.city,
            "company_state": company.state,
            "lost_reason": company.lost_reason,
            "disqualified_by": bdr_name,
            "disqualified_at": act.created_at.isoformat() if act.created_at else None,
        })

    return result


# ============================================================
# Manual company creation + CSV upload
# ============================================================

class CreateCompanyRequest(BaseModel):
    name: str
    website: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    business_type: Optional[str] = None
    linkedin_url: Optional[str] = None
    # Optional first contact
    contact_first_name: Optional[str] = None
    contact_last_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_title: Optional[str] = None
    contact_linkedin: Optional[str] = None
    # Assignment
    assigned_to: Optional[int] = None
    auto_enrich: bool = True


@router.post("/")
async def create_company(
    req: CreateCompanyRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Manually add a company with optional first contact. Auto-enriches if website provided.

    Dedupe-by-domain: if the supplied website normalizes to a domain that already
    matches an existing company, we return that one instead of inserting a duplicate.
    Optional contact info is still created on the existing company so we don't lose
    the BDR's input. Steve hit this on 2026-05-07 with two AAMP Agency rows.
    """
    from app.services.domain_utils import normalize_domain
    new_domain = normalize_domain(req.website)

    # Domain-level dedupe: if a row already exists for this canonical domain,
    # reuse it. We attach the optional contact info onto the existing record.
    existing_company: Optional[Company] = None
    if new_domain:
        existing_company = (await db.execute(
            select(Company).where(Company.domain == new_domain)
        )).scalars().first()

    if existing_company:
        company = existing_company
        merged_contact = None
        if req.contact_first_name or req.contact_email:
            # If a contact with the same email is already on this company, skip;
            # otherwise create a new contact row so we don't lose what the BDR typed.
            dup_contact = None
            if req.contact_email:
                dup_contact = (await db.execute(
                    select(Contact).where(
                        Contact.company_id == company.id,
                        Contact.email == req.contact_email,
                    )
                )).scalar_one_or_none()
            if not dup_contact:
                import secrets as _secrets
                merged_contact = Contact(
                    company_id=company.id,
                    first_name=req.contact_first_name or "",
                    last_name=req.contact_last_name or "",
                    email=req.contact_email,
                    phone=req.contact_phone,
                    title=req.contact_title,
                    linkedin_url=req.contact_linkedin,
                    is_primary=False,
                    unsubscribe_token=_secrets.token_urlsafe(32),
                )
                db.add(merged_contact)
        db.add(Activity(
            company_id=company.id, user_id=user.id,
            activity_type="company_dedup_match",
            content=f"Matched existing company by domain ({new_domain}); contact info merged in instead of creating a duplicate row.",
        ))
        await db.commit()
        await db.refresh(company)
        return {
            "id": company.id, "name": company.name, "status": company.status,
            "deduped": True,
            "matched_by_domain": new_domain,
            "added_contact": bool(merged_contact),
        }

    company = Company(
        name=req.name,
        website=req.website,
        domain=new_domain,
        phone=req.phone,
        address=req.address,
        city=req.city,
        state=req.state,
        business_type=req.business_type,
        linkedin_url=req.linkedin_url,
        assigned_to=req.assigned_to,
        status="new",
    )
    db.add(company)
    await db.flush()

    # Create contact if any contact info provided
    contact = None
    if req.contact_first_name or req.contact_email:
        import secrets as _secrets
        contact = Contact(
            company_id=company.id,
            first_name=req.contact_first_name or "",
            last_name=req.contact_last_name or "",
            email=req.contact_email,
            phone=req.contact_phone,
            title=req.contact_title,
            linkedin_url=req.contact_linkedin,
            is_primary=True,
            unsubscribe_token=_secrets.token_urlsafe(32),
        )
        db.add(contact)

    db.add(Activity(
        company_id=company.id, user_id=user.id,
        activity_type="company_created",
        content=f"Manually added company: {company.name}",
    ))

    await db.commit()
    await db.refresh(company)

    # Auto-enrich in background if website provided
    result = {"id": company.id, "name": company.name, "status": company.status}
    if req.auto_enrich and company.website:
        try:
            # Trigger enrichment (same as the enrich endpoint)
            enrich_result = await enrich_company(company.id, db=db, user=user)
            result["enriched"] = True
            result["problems_found"] = enrich_result.get("problems_found", 0)
        except Exception:
            result["enriched"] = False

    return result


class CSVUploadRow(BaseModel):
    first_name: str = ""
    last_name: str = ""
    company_name: str = ""
    email: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    title: Optional[str] = None
    linkedin_url: Optional[str] = None


class CSVUploadRequest(BaseModel):
    """Accepts EITHER pre-mapped rows (legacy callers) OR raw CSV rows
    with a column-mapping dict.
      rows:    list of dicts already keyed by canonical field names
               (company_name, email, ...). Used by older callers.
      mapping: dict {csv_column_name → canonical_field_name}. When set,
               each row is re-keyed by the mapping before processing —
               so the wizard frontend can keep arbitrary CSV column
               names and tell the backend how to translate.
    Canonical field names: company_name, website, phone, address, city,
      state, first_name, last_name, email, title, linkedin_url.
    Any unmapped CSV columns are stored on the contact's custom_fields_json
    once that ships (TODO).
    """
    rows: list
    mapping: Optional[dict] = None
    assigned_to: Optional[int] = None
    auto_enrich: bool = True
    auto_sequence: bool = True


@router.post("/upload")
async def upload_contacts(
    req: CSVUploadRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """
    Bulk upload contacts with companies. Each row creates a company + contact.
    Optionally auto-enriches and auto-generates sequences.

    When `mapping` is provided, each input row is re-keyed first so the
    wizard frontend can pass raw CSV column names. Unmapped or missing
    keys fall through to the empty-string defaults below.
    """
    import secrets as _secrets

    results = {"created": 0, "skipped": 0, "enriched": 0, "sequences": 0, "errors": []}

    # Canonical row keys the downstream pipeline knows about.
    # Anything else in the mapping is treated as a custom-field key.
    _CANONICAL_FIELDS = {
        "company_name", "website", "phone", "address", "city", "state",
        "first_name", "last_name", "email", "title", "linkedin_url",
    }

    # Apply column mapping (if supplied) — translate raw CSV keys to
    # canonical field names before the row enters the pipeline.
    # Mapping targets that aren't canonical (e.g. 'pool_type') are
    # routed into a special _custom_fields dict on the row, then merged
    # into the company's custom_fields_json after creation.
    if req.mapping:
        normalized_mapping = {
            str(k).strip(): str(v).strip()
            for k, v in req.mapping.items()
            if v and v != "skip"
        }
        # Only allow mappings to known custom field keys for the company
        # entity — typo / stale-def safety. Pre-fetch active defs.
        valid_custom_keys = set((await db.execute(
            select(CustomFieldDefinition.key).where(
                CustomFieldDefinition.entity_type == "company",
                CustomFieldDefinition.is_active == True,
            )
        )).scalars().all())

        translated_rows = []
        for raw in req.rows:
            if not isinstance(raw, dict):
                continue
            translated = {}
            custom_fields = {}
            for csv_col, target in normalized_mapping.items():
                value = None
                for k in raw.keys():
                    if k and str(k).strip().lower() == csv_col.lower():
                        value = raw[k]
                        break
                if value is None:
                    continue
                v = str(value)
                if target in _CANONICAL_FIELDS:
                    translated[target] = v
                elif target in valid_custom_keys:
                    custom_fields[target] = v
                # else: silently drop — typo or stale mapping
            if custom_fields:
                translated["_custom_fields"] = custom_fields
            translated_rows.append(translated)
        rows_to_process = translated_rows
    else:
        rows_to_process = req.rows

    for i, row in enumerate(rows_to_process):
        try:
            company_name = row.get("company_name", "").strip()
            if not company_name:
                results["errors"].append(f"Row {i+1}: missing company name")
                results["skipped"] += 1
                continue

            # Dedup by canonical domain first, then by exact company name. Using the
            # indexed `domain` column avoids the false-positive risk of LIKE '%foo%'
            # (where 'foobar.com' would match 'foo.com').
            from app.services.domain_utils import normalize_domain
            website = row.get("website", "").strip() or None
            new_domain = normalize_domain(website)
            existing = None
            if new_domain:
                existing = (await db.execute(
                    select(Company).where(Company.domain == new_domain)
                )).scalars().first()
            if not existing:
                existing = (await db.execute(
                    select(Company).where(Company.name == company_name)
                )).scalars().first()

            if existing:
                company = existing
            else:
                company = Company(
                    name=company_name,
                    website=website,
                    domain=new_domain,
                    phone=row.get("phone", "").strip() or None,
                    assigned_to=req.assigned_to,
                    status="new",
                )
                db.add(company)
                await db.flush()

            # Merge any custom-field values from the CSV row into the
            # company's custom_fields_json. Existing values are preserved
            # unless the CSV provides a non-empty replacement (admin
            # imports usually overwrite stale data on purpose).
            cf_in = row.get("_custom_fields") or {}
            if cf_in:
                try:
                    existing_cf = json.loads(company.custom_fields_json) if company.custom_fields_json else {}
                except Exception:
                    existing_cf = {}
                for k, v in cf_in.items():
                    if v not in (None, ""):
                        existing_cf[k] = v
                company.custom_fields_json = json.dumps(existing_cf) if existing_cf else None

            # Create contact
            email = row.get("email", "").strip() or None
            first_name = row.get("first_name", "").strip()
            last_name = row.get("last_name", "").strip()

            if email:
                # Check if contact already exists at this company
                existing_contact = (await db.execute(
                    select(Contact).where(Contact.company_id == company.id, Contact.email == email)
                )).scalars().first()

                if not existing_contact:
                    contact = Contact(
                        company_id=company.id,
                        first_name=first_name,
                        last_name=last_name,
                        email=email,
                        phone=row.get("phone", "").strip() or None,
                        title=row.get("title", "").strip() or None,
                        linkedin_url=row.get("linkedin_url", "").strip() or None,
                        is_primary=True,
                        unsubscribe_token=_secrets.token_urlsafe(32),
                    )
                    db.add(contact)
                    await db.flush()

            results["created"] += 1
            await db.commit()

            # Auto-enrich
            if req.auto_enrich and company.website and not company.enriched:
                try:
                    await enrich_company(company.id, db=db, user=user)
                    results["enriched"] += 1
                except Exception:
                    pass

            # Auto-sequence for the primary contact
            if req.auto_sequence and email:
                primary = (await db.execute(
                    select(Contact).where(
                        Contact.company_id == company.id,
                        Contact.email.isnot(None),
                    ).order_by(Contact.is_primary.desc())
                )).scalars().first()

                if primary:
                    existing_emails = (await db.execute(
                        select(GeneratedEmail).where(GeneratedEmail.contact_id == primary.id)
                    )).scalars().first()

                    if not existing_emails:
                        try:
                            from app.engagement_engine.lifecycle import start_engagement
                            n = await start_engagement(
                                db, primary,
                                sequence_label="main",
                                pre_generate_content=True,
                                assigned_bdr_id=req.assigned_to or user.id,
                                initiated_by=f"bulk_import:{user.email[:20]}",
                            )
                            if n > 0:
                                results["sequences"] += 1
                        except Exception:
                            pass

        except Exception as e:
            results["errors"].append(f"Row {i+1}: {str(e)[:80]}")
            results["skipped"] += 1

    return results


@router.get("/{company_id}/full")
async def get_company_full(
    company_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Full company record: contacts (with their email sequences), deals, activities, tags."""
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    from app.scoping import check_company_access
    if not check_company_access(company, user):
        raise HTTPException(status_code=404, detail="Company not found")

    # Lazy lead-score refresh — keeps the score current whenever the user
    # opens the company detail. Cheap (cached after first read within
    # STALE_AFTER) and never breaks the response if it fails.
    try:
        from app.services.lead_scorer import get_or_recompute
        await get_or_recompute(db, company)
    except Exception:
        pass

    # Contacts with their emails
    contacts_result = await db.execute(
        select(Contact).where(Contact.company_id == company_id, Contact.is_archived == False).order_by(Contact.is_primary.desc(), Contact.id)
    )
    contacts = contacts_result.scalars().all()

    # Bulk-load all emails for all contacts in ONE query (avoids N+1 over network)
    # NOTE: The CRM sequence-strip UI reads from this `emails` array. After
    # the engagement-engine cutover, NEW enrollments write to `actions`,
    # NOT `generated_emails`. To keep the UI showing every step (legacy +
    # new), we query both tables, normalize the new-engine actions into
    # the same dict shape the UI expects, and merge them.
    contact_ids = [c.id for c in contacts]
    all_ge = []
    engine_steps_by_contact: dict[int, list] = {}
    if contact_ids:
        all_ge = (await db.execute(
            select(GeneratedEmail)
            .where(GeneratedEmail.contact_id.in_(contact_ids))
            .order_by(GeneratedEmail.sequence_order)
        )).scalars().all()

        # New-engine actions for these contacts. Channel code (email/sms/
        # call_task/linkedin/manual/wait) resolved via channel_types join.
        # ROW_NUMBER over engagement_id by scheduled_at gives each action
        # a stable sequence_order the UI can sort/render.
        from sqlalchemy import text as _sa_text
        action_rows = (await db.execute(_sa_text("""
            SELECT a.id, a.engagement_id, a.contact_id,
                   ct.code AS channel_code,
                   a.status, a.scheduled_at, a.executed_at,
                   a.subject, a.body, a.skip_reason,
                   ROW_NUMBER() OVER (
                       PARTITION BY a.engagement_id
                       ORDER BY a.scheduled_at, a.id
                   ) AS step_order
            FROM actions a
            JOIN channel_types ct ON ct.id = a.channel_id
            WHERE a.contact_id = ANY(:cids)
            ORDER BY a.contact_id, a.scheduled_at
        """), {"cids": contact_ids})).fetchall()

        def _step_type_from_channel(code: str) -> str:
            return {
                "email": "email",
                "sms": "imessage",
                "call_task": "call",
                "linkedin": "linkedin",
                "manual": "manual",
                "wait": "wait",
            }.get(code, code)

        for r in action_rows:
            step = {
                "id": int(r.id),
                "step_type": _step_type_from_channel(r.channel_code),
                "subject": r.subject,
                "body": r.body,
                "email_type": None,
                # Offset sequence_order by 10_000 so action rows sort
                # AFTER any legacy sent steps when the contact has both.
                # The UI sorts by sequence_order then id, so action steps
                # appear in scheduled order under any legacy history.
                "sequence_order": 10000 + int(r.step_order),
                "send_delay_days": None,
                "is_sent": r.status == "sent",
                "paused_at": None,
                "scheduled_send_at": r.scheduled_at.isoformat() if r.scheduled_at else None,
                "sent_at": r.executed_at.isoformat() if r.executed_at else None,
                "created_at": None,
                "skipped_at": r.executed_at.isoformat() if r.status == "skipped" and r.executed_at else None,
                "skip_reason": r.skip_reason,
                "auto_execute": r.channel_code in ("email", "sms"),
                "task_id": None,
                "engine": "engagement_engine",
                "engagement_id": int(r.engagement_id),
            }
            engine_steps_by_contact.setdefault(int(r.contact_id), []).append(step)

    # Group legacy emails by contact
    emails_by_contact: dict[int, list] = {}
    for e in all_ge:
        emails_by_contact.setdefault(e.contact_id, []).append(e)

    contacts_data = []
    for c in contacts:
        all_emails = emails_by_contact.get(c.id, [])
        # Deduplicate: if a sequence was regenerated, old sent steps with the
        # same sequence_order coexist with new pending ones. Keep the newest
        # per sequence_order so the UI doesn't show duplicates.
        seen_orders = {}
        for e in all_emails:
            key = e.sequence_order
            if key in seen_orders:
                prev = seen_orders[key]
                if prev.is_sent and not e.is_sent:
                    seen_orders[key] = e
                elif not prev.is_sent and e.is_sent:
                    pass
                elif e.id > prev.id:
                    seen_orders[key] = e
            else:
                seen_orders[key] = e
        legacy_emails = sorted(seen_orders.values(), key=lambda e: (e.sequence_order or 0, e.id))

        # Merge legacy GeneratedEmail rows with new-engine action steps
        # for this contact. JS renderer sees one unified array, sorted
        # by sequence_order (legacy <10k, engine >10k).
        legacy_dicts = [_email_to_dict(e) for e in legacy_emails]
        engine_dicts = engine_steps_by_contact.get(c.id, [])
        all_steps = legacy_dicts + engine_dicts
        contacts_data.append({
            "id": c.id,
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
            "phone_type_checked_at": c.phone_type_checked_at.isoformat() if c.phone_type_checked_at else None,
            "emails": all_steps,
        })

    # Deals
    deals_result = await db.execute(
        select(Deal).where(Deal.company_id == company_id).order_by(Deal.created_at.desc())
    )
    deals = [
        {
            "id": d.id,
            "name": d.name,
            "value": d.value,
            "stage": d.stage,
            "pipeline": d.pipeline,
            "probability": d.probability,
            "expected_close_date": d.expected_close_date.isoformat() if d.expected_close_date else None,
            "closed_at": d.closed_at.isoformat() if d.closed_at else None,
            "lost_reason": d.lost_reason,
            "assigned_to": d.assigned_to,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in deals_result.scalars().all()
    ]

    # Activities
    activity_result = await db.execute(
        select(Activity).where(Activity.company_id == company_id).order_by(Activity.created_at.desc())
    )
    activities = activity_result.scalars().all()
    user_ids = {a.user_id for a in activities if a.user_id}
    user_names = {}
    if user_ids:
        u_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        for u in u_result.scalars().all():
            user_names[u.id] = u.full_name

    # Tags (explicit query — async SQLAlchemy can't lazy-load relationships)
    tag_result = await db.execute(
        select(Tag)
        .join(company_tags, company_tags.c.tag_id == Tag.id)
        .where(company_tags.c.company_id == company_id)
    )
    tag_list = [{"id": t.id, "name": t.name, "color": t.color} for t in tag_result.scalars().all()]

    # Assigned user
    assigned_name = None
    if company.assigned_to:
        u_result = await db.execute(select(User).where(User.id == company.assigned_to))
        u = u_result.scalar_one_or_none()
        assigned_name = u.full_name if u else None

    problems = json.loads(company.problems_found) if company.problems_found else []
    reviews = json.loads(company.reviews_json) if company.reviews_json else []

    # SoS cache lookup — read-only, never triggers a fresh scrape.
    # The scrape only runs during enrichment so reads are always fast.
    sos_payload = None
    try:
        from app.models import SoSLookup
        from app.services.sos_lookup import _normalize_name
        sos_row = (await db.execute(
            select(SoSLookup).where(
                SoSLookup.state == (company.state or "").upper(),
                SoSLookup.company_name == _normalize_name(company.name or ""),
                SoSLookup.found == True,
            )
        )).scalar_one_or_none()
        if sos_row and sos_row.result_json:
            sos_payload = json.loads(sos_row.result_json)
    except Exception:
        sos_payload = None

    return {
        "id": company.id,
        "name": company.name,
        "phone": company.phone,
        "website": company.website,
        "address": company.address,
        "reviews": reviews,
        "reviews_fetched_at": company.reviews_fetched_at.isoformat() if company.reviews_fetched_at else None,
        "city": company.city,
        "state": company.state,
        "rating": company.rating,
        "review_count": company.review_count,
        "business_type": company.business_type,
        "status": company.status,
        # Sequence-snooze state — drives the snooze banner + button visibility on the UI
        "sequence_resume_at": company.sequence_resume_at.isoformat() if company.sequence_resume_at else None,
        "sequence_snooze_reason": company.sequence_snooze_reason,
        "sequence_snooze_days": company.sequence_snooze_days,
        "sequence_snoozed_at": company.sequence_snoozed_at.isoformat() if company.sequence_snoozed_at else None,
        "enriched": company.enriched,
        "enrichment_summary": company.enrichment_summary,
        "problems_found": problems,
        "problem_count": len(problems),
        "tech_stack": json.loads(company.tech_stack) if company.tech_stack else [],
        "linkedin_url": company.linkedin_url,
        "employee_count": company.employee_count,
        "company_size": company.company_size,
        "industry": company.industry,
        "founded": company.founded,
        "company_description": company.company_description,
        "specialties": company.specialties,
        "follower_count": company.follower_count,
        # First-class social profile URLs auto-scraped from website_intel.
        # Manually editable via PATCH /companies/{id} (treats them as
        # standard string fields).
        "facebook_url": company.facebook_url,
        "instagram_url": company.instagram_url,
        "youtube_url": company.youtube_url,
        "tiktok_url": company.tiktok_url,
        "custom_fields": json.loads(company.custom_fields_json) if company.custom_fields_json else {},
        "sos": sos_payload,
        "company_insights": json.loads(company.company_insights_json) if company.company_insights_json else None,
        "insights_fetched_at": company.insights_fetched_at.isoformat() if company.insights_fetched_at else None,
        "instagram_posts": json.loads(company.instagram_posts_json) if company.instagram_posts_json else None,
        "instagram_posts_fetched_at": company.instagram_posts_fetched_at.isoformat() if company.instagram_posts_fetched_at else None,
        # Tier 2 Netrows caches
        "similarweb": json.loads(company.similarweb_json) if company.similarweb_json else None,
        "similarweb_fetched_at": company.similarweb_fetched_at.isoformat() if company.similarweb_fetched_at else None,
        "monthly_visits": company.monthly_visits,
        "tech_stack": json.loads(company.tech_stack_json) if company.tech_stack_json else None,
        "tech_stack_fetched_at": company.tech_stack_fetched_at.isoformat() if company.tech_stack_fetched_at else None,
        "yelp": json.loads(company.yelp_json) if company.yelp_json else None,
        "yelp_fetched_at": company.yelp_fetched_at.isoformat() if company.yelp_fetched_at else None,
        "indeed_jobs": json.loads(company.indeed_jobs_json) if company.indeed_jobs_json else None,
        "indeed_jobs_fetched_at": company.indeed_jobs_fetched_at.isoformat() if company.indeed_jobs_fetched_at else None,
        "assigned_to": company.assigned_to,
        "assigned_name": assigned_name,
        "tags": tag_list,
        "contacts": contacts_data,
        "deals": deals,
        "timeline": [
            {
                "id": a.id,
                "type": a.activity_type,
                "content": a.content,
                "user_name": user_names.get(a.user_id),
                "metadata": json.loads(a.metadata_json) if a.metadata_json else None,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                # Call-specific fields (only meaningful when type='call' or 'voicemail')
                "twilio_call_sid": a.twilio_call_sid,
                "call_duration_seconds": a.call_duration_seconds,
                "call_direction": a.call_direction,
                "call_outcome": a.call_outcome,
                "has_recording": bool(a.recording_url),
                # Pre-signed streaming URL — token scoped to this activity_id,
                # expires in 30 min. Lets <audio>/wavesurfer play without
                # a bearer header (which media elements can't attach).
                "recording_url": (
                    f"/api/twilio/recording/{a.id}?t={mint_recording_token(a.id, user.id)}"
                    if a.recording_url else None
                ),
                "transcript": a.transcript,
                "call_summary": a.call_summary,
                # Diarization for the dual-channel waveform
                "diarized_segments": (json.loads(a.diarized_segments_json) if a.diarized_segments_json else None),
                "talk_ratio": (json.loads(a.talk_ratio_json) if a.talk_ratio_json else None),
            }
            for a in activities
        ],
        "created_at": company.created_at.isoformat() if company.created_at else None,
        "talking_points": _get_talking_points(company, problems),
    }


def _get_talking_points(company, problems):
    """Generate BDR talking points from enrichment data."""
    try:
        from app.services.talking_points import generate_talking_points

        # Check if we have an audit report for richer data
        serp_competitors = []
        total_kw = 0
        ref_domains = 0
        domain_rank = 0
        has_llms = False
        has_faq = False

        # Extract from problems
        for p in problems:
            ptype = (p.get("type", "") or "").lower()
            if "llms" in ptype:
                has_llms = False
            if "faq" in ptype:
                has_faq = False

        # Check SEO findings for positive signals
        for p in problems:
            if "llms" in (p.get("type", "") or "").lower() and "found" in (p.get("detail", "") or "").lower():
                has_llms = True
            if "faq" in (p.get("type", "") or "").lower() and "found" in (p.get("detail", "") or "").lower():
                has_faq = True

        return generate_talking_points(
            company_name=company.name,
            problems=problems,
            review_count=company.review_count or 0,
            rating=company.rating or 0,
            employee_count=company.employee_count or 0,
            has_llms_txt=has_llms,
            has_faq_schema=has_faq,
        )
    except Exception:
        return []


# ============================================================
# Status updates
# ============================================================

class UpdateStatusRequest(BaseModel):
    status: str


@router.patch("/{company_id}/status")
async def update_company_status(
    company_id: int,
    req: UpdateStatusRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    valid = {"new", "pursuing", "sequencing", "contacted", "replied", "qualified", "converted", "not_interested"}
    if req.status not in valid:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {sorted(valid)}")
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    old = company.status
    company.status = req.status
    db.add(Activity(company_id=company.id, user_id=user.id, activity_type="status_change",
                    content=f"Status: {old} → {req.status}"))
    await db.commit()
    return {"company_id": company.id, "status": company.status}


# ============================================================
# Enrichment
# ============================================================

@router.post("/{company_id}/enrich")
async def enrich_company(
    company_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Crawl website, log marketing problems, look up contacts."""
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    from app.scoping import check_company_access
    if not check_company_access(company, user):
        raise HTTPException(status_code=404, detail="Company not found")
    if not company.website:
        raise HTTPException(status_code=400, detail="Company has no website to analyze")

    # Website analysis
    try:
        analysis = await analyze_website(company.website)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Website analysis failed: {str(e)[:200]}")
    analysis_dict = analysis_to_dict(analysis)
    company.enriched = True
    company.has_blog = analysis.has_blog
    company.has_social_links = analysis.has_social_links
    company.has_ssl = analysis.has_ssl
    company.site_speed_score = analysis.load_time_seconds
    company.mobile_friendly = analysis.mobile_friendly
    company.tech_stack = json.dumps(analysis.tech_stack)
    company.problems_found = json.dumps(analysis.problems)
    company.enrichment_summary = _summarize(analysis)

    # Auto-populate first-class social URL columns from the website scrape.
    # First-write-wins so manual edits via PATCH /companies/{id} aren't
    # clobbered by a later re-enrichment pass.
    su = analysis.social_urls or {}
    if su.get("facebook") and not company.facebook_url:
        company.facebook_url = su["facebook"][:500]
    if su.get("instagram") and not company.instagram_url:
        company.instagram_url = su["instagram"][:500]
    if su.get("youtube") and not company.youtube_url:
        company.youtube_url = su["youtube"][:500]
    if su.get("tiktok") and not company.tiktok_url:
        company.tiktok_url = su["tiktok"][:500]

    # Contact discovery — runs through the EnrichmentWaterfall: Apollo (BYO,
    # if configured) → Netrows decision-makers → Hunter. Each provider
    # meters its own spend. Earlier providers' contacts win on dedup;
    # later providers fill in null fields (e.g. Hunter adds a missing
    # last name on an Apollo-found email).
    from app.services.enrichment_waterfall import EnrichmentWaterfall
    from app.services.credit_meter import meter, make_idem_key

    waterfall = EnrichmentWaterfall()
    waterfall_result = await waterfall.enrich(
        db, domain=(company.website or company.domain or ""),
        company_name=company.name or "",
    )

    # Counters for backward-compat with the existing API response shape
    netrows_added = sum(1 for c in waterfall_result.contacts if c.source == "netrows_dm")
    netrows_found = netrows_added
    hunter_added = sum(1 for c in waterfall_result.contacts if c.source == "hunter")
    hunter_found = hunter_added
    apollo_added = sum(1 for c in waterfall_result.contacts if c.source == "apollo")

    # Persist contacts. _ensure_contact dedupes by email at the company level.
    actually_added = {"netrows_dm": 0, "hunter": 0, "apollo": 0}
    for c in waterfall_result.contacts:
        if not c.email:
            continue
        # Apollo / Netrows often have the cleaner mobile number; fall back to
        # the company's main line so every contact has SOME number to call.
        contact_phone = c.mobile_phone or c.phone or (company.phone or None)
        created = await _ensure_contact(
            db, company_id,
            c.full_name, c.email, c.job_title, contact_phone, c.linkedin_url,
        )
        if created:
            actually_added[c.source] = actually_added.get(c.source, 0) + 1

    netrows_added = actually_added["netrows_dm"]
    hunter_added = actually_added["hunter"]
    apollo_added = actually_added["apollo"]

    # Backfill LinkedIn URLs via Netrows reverse-lookup for contacts
    # that have an email but no LinkedIn profile. 1 credit per lookup.
    try:
        from app.services.netrows_enrichment import reverse_email_lookup
        nr_key = await get_netrows_api_key(db)
        if nr_key:
            no_linkedin = (await db.execute(
                select(Contact).where(
                    Contact.company_id == company_id,
                    Contact.email.isnot(None),
                    Contact.email != "",
                    (Contact.linkedin_url.is_(None)) | (Contact.linkedin_url == ""),
                )
            )).scalars().all()
            for c in no_linkedin[:10]:  # cap at 10 to limit credit spend
                try:
                    rl = await reverse_email_lookup(c.email, nr_key)
                    if rl and rl.linkedin_url:
                        c.linkedin_url = rl.linkedin_url
                        if rl.headline and not c.title:
                            c.title = rl.headline
                except Exception:
                    pass
            if no_linkedin:
                await db.commit()
    except Exception:
        pass  # reverse-lookup failure shouldn't block enrichment

    # Meter Netrows + Hunter at the route level (provider classes don't
    # meter these yet; Apollo meters itself). When the providers fully
    # own metering, this block goes away.
    if "netrows_dm" in waterfall_result.providers_called:
        try:
            await meter(
                db, action_type="enrich_netrows",
                idempotency_key=make_idem_key("enrich_netrows", company_id, "dm"),
                user_id=user.id, action_ref=f"company:{company_id}",
                metadata={"decision_makers": netrows_found, "via": "waterfall"},
            )
        except Exception:
            pass
    if "hunter" in waterfall_result.providers_called:
        try:
            await meter(
                db, action_type="enrich_hunter",
                idempotency_key=make_idem_key("enrich_hunter", company_id),
                user_id=user.id, action_ref=f"company:{company_id}",
                metadata={"contacts_found": hunter_found, "via": "waterfall"},
            )
        except Exception:
            pass

    # Phase 2 enrichment: Netrows premium endpoints
    # /companies/insights — deeper firmographics (revenue range, funding,
    # tech stack, growth signals). Domain-keyed; auto-fires when we have
    # a Netrows API key. Cached on Company.company_insights_json.
    insights_data = None
    try:
        from app.services.netrows_enrichment import company_insights as _netrows_insights
        nr_key = await get_netrows_api_key(db)
        if nr_key and (company.website or company.domain):
            ci = await _netrows_insights(company.website or company.domain, nr_key)
            if ci is not None:
                # Store the raw payload + a curated summary for fast UI render
                payload = {
                    "revenue_range": ci.revenue_range,
                    "funding_stage": ci.funding_stage,
                    "technologies": ci.technologies[:30],
                    "growth_signals": ci.growth_signals[:10],
                    "headcount_growth_pct": ci.headcount_growth_pct,
                    "raw": ci.raw_payload,
                }
                company.company_insights_json = json.dumps(payload, default=str)
                company.insights_fetched_at = datetime.now(timezone.utc)
                insights_data = payload
                try:
                    await meter(
                        db, action_type="enrich_netrows",
                        idempotency_key=make_idem_key("enrich_netrows", company_id, "insights"),
                        user_id=user.id, action_ref=f"company:{company_id}",
                        raw_cost_override_usd=0.055,  # premium endpoint, ~10 credits
                        metadata={"endpoint": "companies/insights"},
                    )
                except Exception:
                    pass
    except Exception as e:
        insights_data = {"error": str(e)[:200]}

    # /instagram/user/posts — recent IG posts for personalization.
    # Only fires when company.instagram_url was scraped from website_intel.
    # 7-day cache TTL since IG posts turn over fast.
    instagram_data = None
    try:
        if company.instagram_url:
            stale = (
                not company.instagram_posts_fetched_at or
                (datetime.now(timezone.utc) - (
                    company.instagram_posts_fetched_at if company.instagram_posts_fetched_at.tzinfo
                    else company.instagram_posts_fetched_at.replace(tzinfo=timezone.utc)
                )).days >= 7
            )
            if stale:
                from app.services.netrows_enrichment import instagram_recent_posts as _netrows_ig
                nr_key = await get_netrows_api_key(db)
                if nr_key:
                    posts = await _netrows_ig(company.instagram_url, nr_key, limit=9)
                    if posts:
                        payload = [{
                            "caption": p.caption, "posted_at": p.posted_at,
                            "url": p.url, "likes": p.likes, "comments": p.comments,
                            "media_type": p.media_type, "thumbnail_url": p.thumbnail_url,
                        } for p in posts]
                        company.instagram_posts_json = json.dumps(payload, default=str)
                        company.instagram_posts_fetched_at = datetime.now(timezone.utc)
                        instagram_data = payload
                        try:
                            await meter(
                                db, action_type="enrich_netrows",
                                idempotency_key=make_idem_key("enrich_netrows", company_id, "instagram"),
                                user_id=user.id, action_ref=f"company:{company_id}",
                                raw_cost_override_usd=0.0055,
                                metadata={"endpoint": "instagram/user/posts"},
                            )
                        except Exception:
                            pass
    except Exception as e:
        instagram_data = {"error": str(e)[:200]}

    # Phase 2 enrichment: Secretary of State lookup (FL Sunbiz, AZ
    # eCorp, NV SilverFlume). Free public-record data — registered
    # agent + officers + filing date + active status. Cached 30 days;
    # only fires for states we have a scraper for. Always best-effort
    # — never blocks the core enrichment flow.
    sos_data = None
    try:
        from app.services.sos_lookup import lookup_state, meter_sos_lookup
        sos_result = await lookup_state(db, company.state, company.name)
        if sos_result and sos_result.found:
            sos_data = sos_result.to_payload()
            await meter_sos_lookup(sos_result.state, company.id)
            # Add officers as Contact rows (no email — Steve's BDRs can
            # research email via Hunter / Netrows after)
            for officer in sos_result.officers[:5]:
                await _ensure_contact(db, company_id,
                                       officer.name, None, officer.title, None, None)
    except Exception as e:
        sos_data = {"error": str(e)[:200]}

    # Phase 2 enrichment: SimilarWeb traffic + tech-stack detection
    # (Tier 2). Both are domain-keyed so we run them whenever we have
    # a website on file, with 30-day cache. monthly_visits gets
    # denormalized for filtering / lead-scoring.
    similarweb_data = None
    tech_stack_data = None
    try:
        if company.website:
            nr_key = await get_netrows_api_key(db)
            if nr_key:
                from app.services.netrows_enrichment import (
                    similarweb_website_overview, technographics_lookup,
                )
                # SimilarWeb — 30-day TTL
                sw_stale = (
                    not company.similarweb_fetched_at or
                    (datetime.now(timezone.utc) - (
                        company.similarweb_fetched_at if company.similarweb_fetched_at.tzinfo
                        else company.similarweb_fetched_at.replace(tzinfo=timezone.utc)
                    )).days >= 30
                )
                if sw_stale:
                    sw = await similarweb_website_overview(company.website, nr_key)
                    if sw:
                        payload = {
                            "domain": sw.domain,
                            "global_rank": sw.global_rank,
                            "country_rank": sw.country_rank,
                            "category_rank": sw.category_rank,
                            "monthly_visits": sw.monthly_visits,
                            "bounce_rate": sw.bounce_rate,
                            "avg_visit_duration_seconds": sw.avg_visit_duration_seconds,
                            "pages_per_visit": sw.pages_per_visit,
                            "top_country": sw.top_country,
                            "top_country_share": sw.top_country_share,
                            "traffic_sources": sw.traffic_sources,
                        }
                        company.similarweb_json = json.dumps(payload, default=str)
                        company.similarweb_fetched_at = datetime.now(timezone.utc)
                        if isinstance(sw.monthly_visits, (int, float)):
                            company.monthly_visits = int(sw.monthly_visits)
                        similarweb_data = payload
                        try:
                            await meter(
                                db, action_type="enrich_netrows",
                                idempotency_key=make_idem_key("enrich_netrows", company_id, "similarweb"),
                                user_id=user.id, action_ref=f"company:{company_id}",
                                raw_cost_override_usd=0.011,
                                metadata={"endpoint": "similarweb/website-overview"},
                            )
                        except Exception:
                            pass
                # Technographics — 30-day TTL
                tech_stale = (
                    not company.tech_stack_fetched_at or
                    (datetime.now(timezone.utc) - (
                        company.tech_stack_fetched_at if company.tech_stack_fetched_at.tzinfo
                        else company.tech_stack_fetched_at.replace(tzinfo=timezone.utc)
                    )).days >= 30
                )
                if tech_stale:
                    tech = await technographics_lookup(company.website, nr_key)
                    if tech:
                        payload = {
                            "url": tech.url,
                            "technologies": tech.technologies,
                            "categories": tech.categories,
                            "cms": tech.cms,
                            "ecommerce": tech.ecommerce,
                            "analytics": tech.analytics,
                            "advertising": tech.advertising,
                        }
                        company.tech_stack_json = json.dumps(payload, default=str)
                        company.tech_stack_fetched_at = datetime.now(timezone.utc)
                        tech_stack_data = payload
                        try:
                            await meter(
                                db, action_type="enrich_netrows",
                                idempotency_key=make_idem_key("enrich_netrows", company_id, "technographics"),
                                user_id=user.id, action_ref=f"company:{company_id}",
                                raw_cost_override_usd=0.011,
                                metadata={"endpoint": "technographics/lookup"},
                            )
                        except Exception:
                            pass
    except Exception as e:
        similarweb_data = {"error": str(e)[:200]}

    # Apply company-level data the waterfall surfaced (employee_count,
    # industry, linkedin_url) when our local fields are still empty.
    cd = waterfall_result.company_data
    if cd.get("employee_count") and not company.employee_count:
        company.employee_count = cd["employee_count"]
    if cd.get("industry") and not company.industry:
        company.industry = cd["industry"]
    if cd.get("linkedin_url") and not company.linkedin_url:
        company.linkedin_url = cd["linkedin_url"]

    # Response payload — keeps the same shape the old code returned plus
    # waterfall-specific fields the UI can surface as provenance.
    netrows_data = {
        "decision_makers": [
            {"email": c.email, "full_name": c.full_name, "job_title": c.job_title,
             "linkedin_url": c.linkedin_url, "email_status": c.email_status}
            for c in waterfall_result.contacts if c.source == "netrows_dm"
        ],
        "error": waterfall_result.errors.get("netrows_dm"),
    }
    hunter_data = {
        "contacts": [
            {"email": c.email,
             "name": (c.full_name or "").strip(),
             "position": c.job_title, "confidence": c.confidence}
            for c in waterfall_result.contacts if c.source == "hunter"
        ],
        "error": waterfall_result.errors.get("hunter"),
    }
    apollo_data = {
        "contacts": [
            {"email": c.email, "full_name": c.full_name, "job_title": c.job_title,
             "linkedin_url": c.linkedin_url, "email_status": c.email_status,
             "mobile_phone": c.mobile_phone, "confidence": c.confidence}
            for c in waterfall_result.contacts if c.source == "apollo"
        ],
        "found": apollo_added,
        "error": waterfall_result.errors.get("apollo"),
    }

    contacts_added = netrows_added + hunter_added + apollo_added

    # Google Maps reviews (1 credit) — owner replies are personalization gold
    if await get_netrows_api_key(db):
        try:
            mr = await netrows_maps_reviews(company.google_place_id or f"{company.name} {company.city or ''}".strip(),
                                             await get_netrows_api_key(db))
            if mr and mr.reviews:
                if mr.place_id and not company.google_place_id:
                    company.google_place_id = mr.place_id
                company.reviews_json = json.dumps([{
                    "author": r.author, "rating": r.rating, "text": r.text,
                    "relative_time": r.relative_time,
                    "owner_reply": r.owner_reply, "owner_reply_time": r.owner_reply_time,
                } for r in mr.reviews])
                company.reviews_fetched_at = datetime.now(timezone.utc)
                await meter(
                    db, action_type="enrich_netrows",
                    idempotency_key=make_idem_key("enrich_netrows", company_id, "maps"),
                    user_id=user.id, action_ref=f"company:{company_id}",
                    raw_cost_override_usd=0.0055,  # 1 credit on Netrows ~ €0.005
                    metadata={"endpoint": "google-maps/reviews"},
                )
        except Exception:
            pass

    # Company enrichment — LinkedIn company profile
    if await get_netrows_api_key(db):
        try:
            ce = await netrows_company_enrich(company.website, await get_netrows_api_key(db), expected_name=company.name)
            if ce:
                if ce.employee_count:
                    company.employee_count = ce.employee_count
                if ce.company_size:
                    company.company_size = ce.company_size
                if ce.industry:
                    company.industry = ce.industry
                if ce.linkedin_url and not company.linkedin_url:
                    company.linkedin_url = ce.linkedin_url
                if ce.founded:
                    company.founded = ce.founded
                if ce.description:
                    company.company_description = ce.description
                if ce.specialties:
                    company.specialties = ce.specialties
                if ce.follower_count:
                    company.follower_count = ce.follower_count
        except Exception:
            pass

    # Local SEO
    seo_data = None
    try:
        seo = await analyze_local_seo(
            company.website,
            business_name=company.name,
            business_type_hint=company.business_type or "home_services",
        )
        seo_data = local_seo_to_dict(seo)
        existing = json.loads(company.problems_found) if company.problems_found else []
        for f in seo.findings:
            existing.append({
                "type": f"seo_{f['issue'].lower().replace(' ', '_')[:30]}",
                "severity": f["category"],
                "detail": f["detail"],
                "angle": f["talking_point"],
            })
        company.problems_found = json.dumps(existing)
        company.enrichment_summary = (company.enrichment_summary or "") + f" Local SEO: {seo.score}/100 | AI Visibility: {seo.ai_visibility_score}/100."
    except Exception:
        pass

    db.add(Activity(
        company_id=company.id, user_id=user.id, activity_type="enriched",
        content=(
            f"Enriched: {len(json.loads(company.problems_found) if company.problems_found else [])} problems · "
            f"{netrows_found + hunter_found} contacts found · "
            f"{netrows_added + hunter_added} added"
        ),
        metadata_json=json.dumps({
            "netrows_found": netrows_found, "netrows_added": netrows_added,
            "hunter_found":  hunter_found,  "hunter_added":  hunter_added,
        }),
    ))
    await db.commit()
    await db.refresh(company)

    return {
        "company_id": company.id,
        "name": company.name,
        "problems_found": len(json.loads(company.problems_found) if company.problems_found else []),
        "contacts_added": contacts_added,
        "netrows_found": netrows_found,
        "netrows_added": netrows_added,
        "hunter_found": hunter_found,
        "hunter_added": hunter_added,
        "apollo_added": apollo_added,
        "analysis": analysis_dict,
        "local_seo": seo_data,
        "summary": company.enrichment_summary,
        "netrows": netrows_data,
        "hunter": hunter_data,
        "apollo": apollo_data,
        "sos": sos_data,
        "insights": insights_data,
        "instagram": instagram_data,
        "waterfall": {
            "providers_called": waterfall_result.providers_called,
            "errors": waterfall_result.errors,
        },
    }


# ============================================================
# Pursue flow — auto-creates Contact + Deal + Sequence
# ============================================================

class PursueRequest(BaseModel):
    company_ids: list[int]


## Use the canonical 13-step template from sequence_engine.DEFAULT_30DAY_TEMPLATE.
## The previous local definition used {"delay_days", "type", ...} keys, but the
## engagement engine reads `tstep.get("day", 0)` and `tstep.get("skip_if", [])`
## from each step — under the local shape every step landed at scheduled_at=now
## with no skip evaluation, so a single BDR-Pursue click fired the whole
## 14-day sequence at once on the next dispatcher tick.
from app.services.sequence_engine import DEFAULT_30DAY_TEMPLATE as SEQUENCE_SCHEDULE


@router.post("/pursue")
async def pursue_companies(
    req: PursueRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """
    For each selected company:
    1. Mark as 'pursuing'
    2. Enrich website + Apollo/Hunter contact lookup
    3. Create primary Contact (if not already)
    4. Generate 4-email sequence FOR THE PRIMARY CONTACT
    5. Create a Deal in stage='prospecting' so it lands on the kanban
    6. Mark as 'sequencing' so the team can review queued messages
    """
    results = []

    for company_id in req.company_ids:
        result = await db.execute(select(Company).where(Company.id == company_id))
        company = result.scalar_one_or_none()
        if not company:
            continue

        company.status = "pursuing"
        await db.commit()

        outcome = {"company_id": company.id, "name": company.name, "steps": []}

        # Step 1: enrich if not already
        if not company.enriched and company.website:
            try:
                analysis = await analyze_website(company.website)
                company.enriched = True
                company.has_blog = analysis.has_blog
                company.has_social_links = analysis.has_social_links
                company.has_ssl = analysis.has_ssl
                company.site_speed_score = analysis.load_time_seconds
                company.mobile_friendly = analysis.mobile_friendly
                company.tech_stack = json.dumps(analysis.tech_stack)
                company.problems_found = json.dumps(analysis.problems)
                company.enrichment_summary = _summarize(analysis)
                # Auto-populate first-class social URL columns
                _su = analysis.social_urls or {}
                if _su.get("facebook") and not company.facebook_url:
                    company.facebook_url = _su["facebook"][:500]
                if _su.get("instagram") and not company.instagram_url:
                    company.instagram_url = _su["instagram"][:500]
                if _su.get("youtube") and not company.youtube_url:
                    company.youtube_url = _su["youtube"][:500]
                if _su.get("tiktok") and not company.tiktok_url:
                    company.tiktok_url = _su["tiktok"][:500]

                # Netrows decision-maker first (verified owner emails for SMB)
                if await get_netrows_api_key(db):
                    try:
                        nr = await netrows_find_decision_makers(company.website, await get_netrows_api_key(db))
                        for dm in nr.decision_makers:
                            await _ensure_contact(db, company.id, dm.full_name, dm.email,
                                                  dm.job_title, None, dm.linkedin_url)
                    except Exception:
                        pass

                # Hunter as additional contact source
                if settings.hunter_api_key:
                    try:
                        hunter = await hunter_search(company.website, settings.hunter_api_key)
                        for hc in hunter.contacts:
                            if hc.email:
                                full = f"{hc.first_name or ''} {hc.last_name or ''}".strip()
                                await _ensure_contact(db, company.id, full, hc.email, hc.position, None, None)
                    except Exception:
                        pass

                try:
                    seo = await analyze_local_seo(company.website, business_name=company.name,
                                                  business_type_hint=company.business_type or "home_services")
                    existing = json.loads(company.problems_found) if company.problems_found else []
                    for f in seo.findings:
                        existing.append({
                            "type": f"seo_{f['issue'].lower().replace(' ', '_')[:30]}",
                            "severity": f["category"],
                            "detail": f["detail"],
                            "angle": f["talking_point"],
                        })
                    company.problems_found = json.dumps(existing)
                    company.enrichment_summary = (company.enrichment_summary or "") + f" Local SEO: {seo.score}/100 | AI Visibility: {seo.ai_visibility_score}/100."
                except Exception:
                    pass

                await db.commit()
                outcome["steps"].append("enriched")
            except Exception as e:
                outcome["steps"].append(f"enrichment_failed: {str(e)[:60]}")

        # Step 2: get the primary contact
        primary = await _get_primary_contact(db, company.id)
        if not primary:
            primary = Contact(
                company_id=company.id,
                first_name="", last_name="",
                is_primary=True,
                unsubscribe_token=secrets.token_urlsafe(24),
            )
            db.add(primary)
            await db.flush()

        # Step 3: enroll the primary contact via the engagement engine.
        # All the per-step generation, audit URL injection, skip-if
        # evaluation, and recipient locking lives inside
        # lifecycle.start_engagement — same template, single call.
        problems = json.loads(company.problems_found) if company.problems_found else []
        if problems:
            from app.engagement_engine.lifecycle import start_engagement
            audit_url = None
            try:
                from app.services.audit_report import ensure_audit_for_company
                audit_url = await ensure_audit_for_company(db, company)
            except Exception:
                pass

            try:
                emails_created = await start_engagement(
                    db, primary,
                    template=SEQUENCE_SCHEDULE,
                    sequence_label="main",
                    pre_generate_content=True,
                    assigned_bdr_id=company.assigned_to or user.id,
                    initiated_by=f"manual_pursue:{user.email[:20]}",
                )
            except Exception as _e:
                emails_created = 0

            # Auto-create Deal so it appears on the kanban
            existing_deal = (await db.execute(
                select(Deal).where(Deal.company_id == company.id,
                                   Deal.stage.in_(await _pipeline_cfg_pursue.get_open_stage_keys(db)))
            )).scalar_one_or_none()
            if not existing_deal:
                from app.routes.deal_routes import recommend_package
                pkg = recommend_package(company.employee_count)
                deal = Deal(
                    company_id=company.id,
                    name=f"{company.name} — Initial Deal",
                    value=0,  # No value until they engage
                    package=pkg,
                    contract_months=6,
                    stage="in_sequence",
                    probability=0,
                    assigned_to=user.id,
                )
                db.add(deal)
                await db.flush()
                db.add(Activity(company_id=company.id, user_id=user.id, activity_type="deal_created",
                                content=f"Deal created in pipeline: {deal.name}"))

            db.add(Activity(company_id=company.id, user_id=user.id, activity_type="sequence_created",
                            content=f"Sequence created for {primary.full_name or primary.email or 'primary contact'} ({emails_created} emails)",
                            metadata_json=json.dumps({"contact_id": primary.id, "emails": emails_created})))
            try:
                from app.services.webhook_dispatch import dispatch_event
                await dispatch_event(db, "sequence.created", {
                    "contact_id": primary.id,
                    "company_id": company.id,
                    "company_name": company.name,
                    "contact_email": primary.email,
                    "step_count": emails_created,
                    "kind": "pursue",
                })
            except Exception:
                pass

            # Audit is now generated UP FRONT by ensure_audit_for_company
            # at the top of this handler, and threaded into the email +
            # iMessage generators directly. No post-hoc URL injection
            # needed; the AI already wove the link into the body where
            # it makes most sense.
            if audit_url:
                outcome["steps"].append("audit_generated")

            await db.commit()
            outcome["steps"].append(f"sequence_created ({emails_created} emails)")
            outcome["steps"].append("deal_created")

        results.append(outcome)

    return {"pursued": len(results), "results": results}


# ============================================================
# Helpers
# ============================================================

async def _ensure_contact(
    db: AsyncSession, company_id: int,
    name: str | None, email: str | None, title: str | None,
    phone: str | None, linkedin: str | None,
) -> Contact | None:
    """Create a Contact if no existing one matches by email; return the new contact (or None if duplicate)."""
    if email:
        existing = (await db.execute(
            select(Contact).where(Contact.company_id == company_id, Contact.email == email)
        )).scalar_one_or_none()
        if existing:
            return None

    first, last = _split_name(name)
    # If enrichment didn't return a name, try to infer from the email address
    if not first and email:
        first, last = _infer_name_from_email(email)
    has_primary = (await db.execute(
        select(Contact).where(Contact.company_id == company_id, Contact.is_primary == True)
    )).scalar_one_or_none()

    contact = Contact(
        company_id=company_id,
        first_name=first, last_name=last,
        title=title or None,
        email=email or None,
        phone=phone or None,
        linkedin_url=linkedin or None,
        is_primary=(has_primary is None),
        unsubscribe_token=secrets.token_urlsafe(24),
    )
    db.add(contact)
    await db.flush()
    return contact


async def _get_primary_contact(db: AsyncSession, company_id: int) -> Contact | None:
    return (await db.execute(
        select(Contact)
        .where(Contact.company_id == company_id)
        .order_by(Contact.is_primary.desc(), Contact.id)
    )).scalar_one_or_none()


def _split_name(full: str | None) -> tuple[str, str]:
    if not full:
        return "", ""
    parts = full.strip().split(maxsplit=1)
    return (parts[0], "") if len(parts) == 1 else (parts[0], parts[1])


# Generic/role prefixes that should NOT be treated as a person's name.
_GENERIC_PREFIXES = frozenset({
    "info", "hello", "contact", "admin", "support", "sales", "office",
    "billing", "service", "team", "help", "marketing", "construction",
    "accounting", "hr", "jobs", "careers", "general", "mail", "enquiries",
    "inquiries", "noreply", "no-reply", "notifications", "ops",
    "processing", "design", "employment", "estimating", "dispatch",
    "tellmemore", "asktheuglypool", "keywize", "tomgood", "ar", "ap",
    "orders", "purchasing", "webmaster", "postmaster", "abuse",
    "reception", "feedback", "media", "press", "events", "booking",
    "reservations", "payments",
})


def _infer_name_from_email(email: str) -> tuple[str, str]:
    """Try to extract a first/last name from an email local part.

    Patterns handled:
      jake.wozniak@domain   → Jake, Wozniak
      jake_wozniak@domain   → Jake, Wozniak
      jwozniak@domain       → (skip — ambiguous initial)
      jake@domain           → Jake, ''
      construction@domain   → '', '' (generic)
    """
    if not email or "@" not in email:
        return "", ""
    local = email.split("@")[0].lower().strip()
    # Skip if it's a generic/role address
    if local in _GENERIC_PREFIXES:
        return "", ""
    # Split on . or _ or -
    import re
    parts = re.split(r'[._\-]', local)
    parts = [p for p in parts if p and len(p) > 1]  # drop single-char initials
    if not parts:
        return "", ""
    # Filter out generic parts
    parts = [p for p in parts if p not in _GENERIC_PREFIXES]
    if not parts:
        return "", ""
    first = parts[0].capitalize()
    last = parts[1].capitalize() if len(parts) > 1 else ""
    # Sanity: if "first name" looks like a number or random chars, skip
    if not first.isalpha():
        return "", ""
    return first, last


def _company_summary(c: Company, assigned_name: Optional[str] = None, tags: Optional[list] = None, sequence_next_step: Optional[int] = None, contact_count: int = 0) -> dict:
    problems = json.loads(c.problems_found) if c.problems_found else []
    return {
        "id": c.id,
        "search_id": c.search_id,
        "assigned_to": c.assigned_to,
        "assigned_name": assigned_name,
        "name": c.name,
        "phone": c.phone,
        "website": c.website,
        "address": c.address,
        "city": c.city,
        "state": c.state,
        "rating": c.rating,
        "review_count": c.review_count,
        "business_type": c.business_type,
        "enriched": c.enriched,
        "problems_found": problems,
        "problem_count": len(problems),
        "enrichment_summary": c.enrichment_summary,
        "tech_stack": json.loads(c.tech_stack) if c.tech_stack else [],
        "has_blog": c.has_blog,
        "has_social_links": c.has_social_links,
        "site_speed_score": c.site_speed_score,
        "status": c.status,
        "sequence_resume_at": c.sequence_resume_at.isoformat() if c.sequence_resume_at else None,
        "sequence_snooze_reason": c.sequence_snooze_reason,
        "email_generated": c.email_generated,
        "employee_count": c.employee_count,
        "company_size": c.company_size,
        "industry": c.industry,
        "linkedin_url": c.linkedin_url,
        "founded": c.founded,
        "company_description": c.company_description,
        "specialties": c.specialties,
        "follower_count": c.follower_count,
        "facebook_url": c.facebook_url,
        "instagram_url": c.instagram_url,
        "youtube_url": c.youtube_url,
        "tiktok_url": c.tiktok_url,
        # Lead score v2 (cached). Recomputed lazily on /companies/{id}/full
        # reads when stale; the dashboard hot-leads sweep also forces a
        # refresh for any company with new engagement activity.
        "lead_score": c.lead_score or 0,
        "lead_score_tier": c.lead_score_tier or "cold",
        "lead_score_fit": c.lead_score_fit or 0,
        "lead_score_intent": c.lead_score_intent or 0,
        "lead_score_components": json.loads(c.lead_score_components) if c.lead_score_components else {},
        "lead_score_updated_at": c.lead_score_updated_at.isoformat() if c.lead_score_updated_at else None,
        # Tenant-defined custom field values (Facebook, Instagram, annual
        # revenue, etc). Field definitions live in custom_field_definitions.
        "custom_fields": json.loads(c.custom_fields_json) if c.custom_fields_json else {},
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        "tags": tags or [],
        "sequence_next_step": sequence_next_step,
        "contact_count": contact_count,
    }


def _email_to_dict(e: GeneratedEmail) -> dict:
    return {
        "id": e.id,
        "step_type": e.step_type or "email",
        "subject": e.subject,
        "body": e.body,
        "email_type": e.email_type,
        "sequence_order": e.sequence_order,
        "send_delay_days": e.send_delay_days,
        "is_sent": e.is_sent,
        "paused_at": e.paused_at.isoformat() if e.paused_at else None,
        "scheduled_send_at": e.scheduled_send_at.isoformat() if e.scheduled_send_at else None,
        "sent_at": e.sent_at.isoformat() if e.sent_at else None,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        # Skip + auto fields — drive the progress-strip color coding
        # (gray=skipped, red=stalled-auto, orange=task-awaiting-BDR).
        "skipped_at": e.skipped_at.isoformat() if e.skipped_at else None,
        "skip_reason": e.skip_reason,
        "auto_execute": bool(e.auto_execute),
        "task_id": e.task_id,
    }


# ============================================================
# On-demand reviews refresh (Netrows /google-maps/reviews — 1 credit)
# ============================================================

@router.post("/{company_id}/refresh-reviews")
async def refresh_reviews(
    company_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if not await get_netrows_api_key(db):
        raise HTTPException(status_code=400, detail="Data enrichment not configured")

    seed = company.google_place_id or f"{company.name} {company.city or ''}".strip()
    mr = await netrows_maps_reviews(seed, await get_netrows_api_key(db))
    if not mr or not mr.reviews:
        return {"reviews_count": 0, "owner_replies_count": 0, "message": "No reviews found"}

    if mr.place_id and not company.google_place_id:
        company.google_place_id = mr.place_id
    company.reviews_json = json.dumps([{
        "author": r.author, "rating": r.rating, "text": r.text,
        "relative_time": r.relative_time,
        "owner_reply": r.owner_reply, "owner_reply_time": r.owner_reply_time,
    } for r in mr.reviews])
    company.reviews_fetched_at = datetime.now(timezone.utc)
    await db.commit()

    owner_replies = sum(1 for r in mr.reviews if r.owner_reply)
    return {
        "reviews_count": len(mr.reviews),
        "owner_replies_count": owner_replies,
        "fetched_at": company.reviews_fetched_at.isoformat(),
    }


@router.post("/{company_id}/clear-enrichment")
async def clear_company_enrichment(
    company_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Clear Netrows-derived company-level enrichment fields so the next
    enrich call rebuilds cleanly. Used to recover from cases where
    Netrows mapped a domain to the wrong company (e.g. proficientpatios.com
    → 'Proficient Audio'). Doesn't touch contacts, deals, sequences, or
    website-scrape data — just the fields that come from Netrows
    /companies/by-domain + /companies/details + /companies/insights."""
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    from app.scoping import check_company_access
    if not check_company_access(company, user):
        raise HTTPException(status_code=404, detail="Company not found")

    cleared = []
    for field_name in (
        "employee_count", "company_size", "industry", "founded",
        "company_description", "specialties", "follower_count",
        "linkedin_url", "company_insights_json", "insights_fetched_at",
    ):
        if getattr(company, field_name) not in (None, "", 0):
            setattr(company, field_name, None)
            cleared.append(field_name)
    db.add(Activity(
        company_id=company.id, user_id=user.id,
        activity_type="enrichment_cleared",
        content=f"Cleared enrichment-derived fields ({len(cleared)}): {', '.join(cleared) or 'none'}",
    ))
    await db.commit()
    return {"cleared_fields": cleared, "message": "Re-enrich now to rebuild from scratch"}


@router.post("/{company_id}/refresh-instagram-posts")
async def refresh_instagram_posts(
    company_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Force-refresh Instagram posts for a company. Skips the 7-day TTL
    check the auto-fetch path uses. Useful when the BDR wants the latest
    posts for personalization right now."""
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if not company.instagram_url:
        raise HTTPException(status_code=400, detail="No Instagram URL on this company. Add one or re-enrich to scrape it from the website.")
    nr_key = await get_netrows_api_key(db)
    if not nr_key:
        raise HTTPException(status_code=400, detail="Data enrichment not configured")

    from app.services.netrows_enrichment import instagram_recent_posts as _ig
    from app.services.credit_meter import meter, make_idem_key

    posts = await _ig(company.instagram_url, nr_key, limit=9)
    if not posts:
        return {"count": 0, "message": "No Instagram posts found (private profile or invalid handle)"}
    payload = [{
        "caption": p.caption, "posted_at": p.posted_at,
        "url": p.url, "likes": p.likes, "comments": p.comments,
        "media_type": p.media_type, "thumbnail_url": p.thumbnail_url,
    } for p in posts]
    company.instagram_posts_json = json.dumps(payload, default=str)
    company.instagram_posts_fetched_at = datetime.now(timezone.utc)
    try:
        await meter(
            db, action_type="enrich_netrows",
            idempotency_key=make_idem_key("enrich_netrows", company_id, "instagram_manual",
                                          datetime.now(timezone.utc).timestamp()),
            user_id=user.id, action_ref=f"company:{company_id}",
            raw_cost_override_usd=0.0055,
            metadata={"endpoint": "instagram/user/posts", "trigger": "manual"},
        )
    except Exception:
        pass
    await db.commit()
    return {
        "count": len(posts),
        "fetched_at": company.instagram_posts_fetched_at.isoformat(),
        "posts": payload,
    }


@router.post("/{company_id}/refresh-insights")
async def refresh_company_insights(
    company_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Force-refresh Netrows /companies/insights for a company. Premium
    endpoint — deeper firmographics + tech stack + growth signals."""
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if not (company.website or company.domain):
        raise HTTPException(status_code=400, detail="Company has no website / domain to look up")
    nr_key = await get_netrows_api_key(db)
    if not nr_key:
        raise HTTPException(status_code=400, detail="Data enrichment not configured")

    from app.services.netrows_enrichment import company_insights as _insights
    from app.services.credit_meter import meter, make_idem_key

    ci = await _insights(company.website or company.domain, nr_key)
    if ci is None:
        return {"found": False, "message": "Insights returned no data — domain may not be in our data pipeline"}
    payload = {
        "revenue_range": ci.revenue_range,
        "funding_stage": ci.funding_stage,
        "technologies": ci.technologies[:30],
        "growth_signals": ci.growth_signals[:10],
        "headcount_growth_pct": ci.headcount_growth_pct,
        "raw": ci.raw_payload,
    }
    company.company_insights_json = json.dumps(payload, default=str)
    company.insights_fetched_at = datetime.now(timezone.utc)
    try:
        await meter(
            db, action_type="enrich_netrows",
            idempotency_key=make_idem_key("enrich_netrows", company_id, "insights_manual",
                                          datetime.now(timezone.utc).timestamp()),
            user_id=user.id, action_ref=f"company:{company_id}",
            raw_cost_override_usd=0.055,
            metadata={"endpoint": "companies/insights", "trigger": "manual"},
        )
    except Exception:
        pass
    await db.commit()
    return {"found": True, "fetched_at": company.insights_fetched_at.isoformat(), **payload}


@router.post("/{company_id}/refresh-yelp")
async def refresh_company_yelp(
    company_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Pull Yelp profile + recent reviews for a company. Two-step:
    search by name + city → details + reviews (owner replies highlighted).
    Owner replies are gold for personalization ('I see how you handled
    that one-star — let's talk')."""
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if not (company.name and (company.city or company.state)):
        raise HTTPException(status_code=400, detail="Company needs name + city/state for Yelp search")
    nr_key = await get_netrows_api_key(db)
    if not nr_key:
        raise HTTPException(status_code=400, detail="Data enrichment not configured")

    from app.services.netrows_enrichment import (
        yelp_business_search, yelp_business_details, yelp_business_reviews,
    )
    from app.services.credit_meter import meter, make_idem_key

    location = ", ".join(p for p in [company.city, company.state] if p)
    matches = await yelp_business_search(company.name, location, nr_key, limit=5)
    if not matches:
        return {"found": False, "message": "No Yelp results matched this company"}
    # Pick top match — same defensive posture as enrich_company_by_domain:
    # if the top result name has zero token overlap with our company name,
    # bail rather than guess.
    top = matches[0]
    a, b = (top.name or "").lower(), (company.name or "").lower()
    overlap = len(set(a.split()) & set(b.split()))
    if overlap == 0:
        return {"found": False, "message": f"Top Yelp match '{top.name}' doesn't match — bailing rather than mismatch"}

    detail = await yelp_business_details(top.alias, nr_key) if top.alias else top
    reviews = []
    if detail and detail.biz_id and detail.alias:
        reviews = await yelp_business_reviews(detail.biz_id, detail.alias, nr_key, limit=20)

    payload = {
        "alias": (detail or top).alias,
        "biz_id": (detail or top).biz_id,
        "name": (detail or top).name,
        "phone": (detail or top).phone,
        "website": (detail or top).website,
        "yelp_url": (detail or top).yelp_url,
        "rating": (detail or top).rating,
        "review_count": (detail or top).review_count,
        "price_range": (detail or top).price_range,
        "categories": (detail or top).categories,
        "address": (detail or top).address,
        "city": (detail or top).city,
        "state": (detail or top).state,
        "zip_code": (detail or top).zip_code,
        "hours_summary": (detail or top).hours_summary,
        "photo_url": (detail or top).photo_url,
        "reviews": [{
            "rating": r.rating, "text": r.text, "posted_at": r.posted_at,
            "reviewer_name": r.reviewer_name, "reviewer_profile_url": r.reviewer_profile_url,
            "owner_response": r.owner_response, "owner_response_at": r.owner_response_at,
            "review_url": r.review_url,
        } for r in reviews],
    }
    company.yelp_json = json.dumps(payload, default=str)
    company.yelp_fetched_at = datetime.now(timezone.utc)
    try:
        await meter(
            db, action_type="enrich_netrows",
            idempotency_key=make_idem_key("enrich_netrows", company_id, "yelp_manual",
                                          datetime.now(timezone.utc).timestamp()),
            user_id=user.id, action_ref=f"company:{company_id}",
            raw_cost_override_usd=0.022,  # ~3 endpoint calls
            metadata={"endpoint": "yelp/*", "trigger": "manual"},
        )
    except Exception:
        pass
    await db.commit()
    return {"found": True, "fetched_at": company.yelp_fetched_at.isoformat(), **payload}


@router.post("/{company_id}/refresh-indeed")
async def refresh_company_indeed(
    company_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Indeed jobs for a company. Hiring activity = budget signal.
    Search filters by company name + city — caller's burden to interpret
    'no jobs found' (could mean truly not hiring, or company isn't on
    Indeed at all)."""
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if not company.name:
        raise HTTPException(status_code=400, detail="Company name required")
    nr_key = await get_netrows_api_key(db)
    if not nr_key:
        raise HTTPException(status_code=400, detail="Data enrichment not configured")

    from app.services.netrows_enrichment import indeed_jobs_for_company
    from app.services.credit_meter import meter, make_idem_key

    location = ", ".join(p for p in [company.city, company.state] if p) or None
    jobs = await indeed_jobs_for_company(company.name, nr_key, location=location)
    payload = {"jobs": [{
        "title": j.title, "company": j.company, "location": j.location,
        "posted_at": j.posted_at, "job_url": j.job_url, "salary": j.salary,
        "job_type": j.job_type, "snippet": j.snippet,
    } for j in jobs]}
    company.indeed_jobs_json = json.dumps(payload, default=str)
    company.indeed_jobs_fetched_at = datetime.now(timezone.utc)
    try:
        await meter(
            db, action_type="enrich_netrows",
            idempotency_key=make_idem_key("enrich_netrows", company_id, "indeed_manual",
                                          datetime.now(timezone.utc).timestamp()),
            user_id=user.id, action_ref=f"company:{company_id}",
            raw_cost_override_usd=0.011,
            metadata={"endpoint": "indeed/job-search", "trigger": "manual"},
        )
    except Exception:
        pass
    await db.commit()
    return {"found": bool(jobs), "fetched_at": company.indeed_jobs_fetched_at.isoformat(), **payload}


def _summarize(analysis) -> str:
    problems = analysis.problems
    if not problems:
        return "No major issues found — this business has a solid web presence."
    crit = [p for p in problems if p["severity"] == "critical"]
    high = [p for p in problems if p["severity"] == "high"]
    med = [p for p in problems if p["severity"] == "medium"]
    parts = []
    if crit: parts.append(f"{len(crit)} critical issue(s)")
    if high: parts.append(f"{len(high)} high-priority issue(s)")
    if med:  parts.append(f"{len(med)} improvement opportunity(ies)")
    summary = f"Found {', '.join(parts)}. "
    if problems:
        summary += f"Top issue: {problems[0]['detail']}"
    return summary


# ============================================================
# Merge companies — combine duplicates into one canonical record
# ============================================================

class MergeCompaniesRequest(BaseModel):
    keep_id: int
    merge_from_ids: list[int]


# Tables that have a company_id FK we need to re-point during a merge.
# (Static list — adding a new table requires updating this. Documented in the
# model file: Activity, Contact, Deal, GeneratedEmail, PageView, Task,
# TrackingLink. Plus the company_tags association.)
_MERGE_REPOINT_TABLES = ["activities", "contacts", "deals", "generated_emails", "page_views", "tasks", "tracking_links"]


@router.post("/merge")
async def merge_companies(
    req: MergeCompaniesRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Merge one or more companies into a kept company.

    What happens:
      1. All child rows on the merge-from companies are re-pointed to keep_id
         (Activities, Contacts, Deals, GeneratedEmails, Tasks, TrackingLinks,
         PageViews — everything with a company_id FK).
      2. Tags from the merged-from companies are unioned onto the kept one.
      3. Empty fields on the kept company are backfilled from the first
         merge-from row that has a non-empty value (linkedin_url, phone,
         address bits, problems_found, etc.). Non-empty kept fields are NOT
         overwritten — kept wins on conflict.
      4. The merged-from company rows are deleted.
      5. An Activity row is logged on the kept company recording the merge.

    Idempotent against re-runs: if you merge A+B → A and call again, A is
    unchanged (B no longer exists)."""
    from sqlalchemy import text as sql_text

    # Admin-only — destructive operation that deletes Company rows + re-points
    # every child table. A sales_rep accidentally clicking through this could
    # destroy data; require admin to gate it.
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if req.keep_id in req.merge_from_ids:
        raise HTTPException(status_code=400, detail="keep_id can't also appear in merge_from_ids")
    if not req.merge_from_ids:
        raise HTTPException(status_code=400, detail="Pass at least one merge_from_id")

    keep = (await db.execute(select(Company).where(Company.id == req.keep_id))).scalar_one_or_none()
    if not keep:
        raise HTTPException(status_code=404, detail="keep_id company not found")

    merge_from = (await db.execute(select(Company).where(Company.id.in_(req.merge_from_ids)))).scalars().all()
    if len(merge_from) != len(req.merge_from_ids):
        found = {c.id for c in merge_from}
        missing = [i for i in req.merge_from_ids if i not in found]
        raise HTTPException(status_code=404, detail=f"Some merge_from_ids not found: {missing}")

    # Backfill empty fields on the kept company from the merge-from rows.
    # Only nullable string/text fields — booleans + counts + json blobs we
    # leave alone; the kept row's values are authoritative.
    backfill_fields = [
        "website", "phone", "address", "city", "state", "zip_code",
        "linkedin_url", "instagram_url", "facebook_url", "twitter_url",
        "industry", "business_type", "company_description", "specialties",
        "founded", "company_size", "google_place_id", "problems_found",
    ]
    backfilled = []
    for f in backfill_fields:
        if not hasattr(keep, f):
            continue
        if (getattr(keep, f) or "").strip() if isinstance(getattr(keep, f), str) else getattr(keep, f):
            continue  # kept already has a value
        for src in merge_from:
            v = getattr(src, f, None)
            if v not in (None, ""):
                setattr(keep, f, v)
                backfilled.append(f)
                break

    # Re-point all child tables to keep_id (raw SQL — single round-trip per table)
    repoint_counts: dict[str, int] = {}
    for tbl in _MERGE_REPOINT_TABLES:
        # Use IN-clause; SQLite handles up to 999 params per statement, plenty.
        placeholders = ",".join(f":id{i}" for i in range(len(req.merge_from_ids)))
        params = {"keep": req.keep_id, **{f"id{i}": v for i, v in enumerate(req.merge_from_ids)}}
        result = await db.execute(
            sql_text(f"UPDATE {tbl} SET company_id = :keep WHERE company_id IN ({placeholders})"),
            params,
        )
        repoint_counts[tbl] = result.rowcount or 0

    # Union tags via the association table — same IN-clause pattern, but
    # tags from merge-from rows that already exist on the kept company
    # would violate the (company_id, tag_id) PK. ON CONFLICT skips dupes
    # on Postgres (and the equivalent OR IGNORE on SQLite).
    placeholders = ",".join(f":id{i}" for i in range(len(req.merge_from_ids)))
    params = {"keep": req.keep_id, **{f"id{i}": v for i, v in enumerate(req.merge_from_ids)}}
    await db.execute(
        sql_text(f"""
            INSERT INTO company_tags (company_id, tag_id)
            SELECT :keep, tag_id FROM company_tags WHERE company_id IN ({placeholders})
            ON CONFLICT (company_id, tag_id) DO NOTHING
        """),
        params,
    )
    await db.execute(
        sql_text(f"DELETE FROM company_tags WHERE company_id IN ({placeholders})"),
        {f"id{i}": v for i, v in enumerate(req.merge_from_ids)},
    )

    # Now safe to delete the merged-from company rows
    deleted_names = [c.name for c in merge_from]
    for src in merge_from:
        await db.delete(src)

    # Audit trail
    db.add(Activity(
        company_id=keep.id,
        user_id=user.id,
        activity_type="company_merged",
        content=f"Merged {len(merge_from)} duplicate(s) into this company: {', '.join(deleted_names)}",
        metadata_json=json.dumps({
            "merged_from_ids": req.merge_from_ids,
            "merged_from_names": deleted_names,
            "repoint_counts": repoint_counts,
            "backfilled_fields": backfilled,
        }),
    ))

    await db.commit()
    await db.refresh(keep)

    # Audit log + outbound webhook
    try:
        from app.services.audit_log import record_audit
        await record_audit(
            db, actor=user, action="company.merged",
            target_type="company", target_id=keep.id, target_label=keep.name,
            metadata={
                "kept_id": keep.id,
                "merged_from_ids": req.merge_from_ids,
                "merged_from_names": deleted_names,
                "repoint_counts": repoint_counts,
            },
        )
        await db.commit()
    except Exception:
        pass
    try:
        from app.services.webhook_dispatch import dispatch_event
        await dispatch_event(db, "company.merged", {
            "kept_id": keep.id,
            "kept_name": keep.name,
            "merged_from_ids": req.merge_from_ids,
            "merged_from_names": deleted_names,
            "repoint_counts": repoint_counts,
        })
    except Exception:
        pass

    return {
        "kept_id": keep.id,
        "kept_name": keep.name,
        "merged_count": len(merge_from),
        "merged_names": deleted_names,
        "repoint_counts": repoint_counts,
        "backfilled_fields": backfilled,
    }


# ============================================================
# Bulk actions on Companies (admin) — assign / tag / enrich / status / delete
# ============================================================

class BulkCompanyActionRequest(BaseModel):
    company_ids: list[int]
    action: str        # 'assign' | 'add_tag' | 'remove_tag' | 'set_status' | 'enrich' | 'delete'
    # Action-specific payload
    user_id: Optional[int] = None        # for 'assign'
    tag_id: Optional[int] = None         # for 'add_tag' / 'remove_tag'
    status: Optional[str] = None         # for 'set_status'


@router.post("/batch")
async def bulk_company_action(
    req: BulkCompanyActionRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Apply an action to many companies in one call. Admin/super_admin only.
    Mirrors the Companies multi-select bar UX. Designed to handle 1-500 IDs
    cleanly — beyond that, batch on the client side.

    Actions:
      - 'assign'      : set assigned_to = user_id
      - 'add_tag'     : insert (company_id, tag_id) into company_tags (idempotent)
      - 'remove_tag'  : delete that row
      - 'set_status'  : update status field (validated against known set)
      - 'enrich'      : fire enrich_company in the background for each (best-effort)
      - 'delete'      : drop the company + cascade children (DESTRUCTIVE)
    """
    from sqlalchemy import text as sql_text

    if user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    if not req.company_ids:
        raise HTTPException(status_code=400, detail="company_ids must be non-empty")
    if len(req.company_ids) > 500:
        raise HTTPException(status_code=400, detail="Too many IDs in one batch (max 500)")

    placeholders = ",".join(f":id{i}" for i in range(len(req.company_ids)))
    id_params = {f"id{i}": v for i, v in enumerate(req.company_ids)}
    affected = 0
    errors: list[str] = []

    if req.action == "assign":
        # user_id may be None to unassign
        result = await db.execute(
            sql_text(f"UPDATE companies SET assigned_to = :uid WHERE id IN ({placeholders})"),
            {"uid": req.user_id, **id_params},
        )
        affected = result.rowcount or 0
        # Audit Activity per company
        for cid in req.company_ids:
            db.add(Activity(
                company_id=cid, user_id=user.id,
                activity_type="bulk_assigned",
                content=f"Bulk assigned to user_id={req.user_id}" if req.user_id else "Bulk unassigned",
            ))

    elif req.action == "add_tag":
        if not req.tag_id:
            raise HTTPException(status_code=400, detail="tag_id required")
        # ON CONFLICT — composite PK (company_id, tag_id) auto-dedupes
        await db.execute(
            sql_text(f"""
                INSERT INTO company_tags (company_id, tag_id)
                SELECT id, :tid FROM companies WHERE id IN ({placeholders})
                ON CONFLICT (company_id, tag_id) DO NOTHING
            """),
            {"tid": req.tag_id, **id_params},
        )
        affected = len(req.company_ids)

    elif req.action == "remove_tag":
        if not req.tag_id:
            raise HTTPException(status_code=400, detail="tag_id required")
        result = await db.execute(
            sql_text(f"DELETE FROM company_tags WHERE tag_id = :tid AND company_id IN ({placeholders})"),
            {"tid": req.tag_id, **id_params},
        )
        affected = result.rowcount or 0

    elif req.action == "set_status":
        valid = {"new", "pursuing", "sequencing", "contacted", "replied", "qualified", "converted", "not_interested"}
        if req.status not in valid:
            raise HTTPException(status_code=400, detail=f"status must be one of {sorted(valid)}")
        result = await db.execute(
            sql_text(f"UPDATE companies SET status = :s WHERE id IN ({placeholders})"),
            {"s": req.status, **id_params},
        )
        affected = result.rowcount or 0
        for cid in req.company_ids:
            db.add(Activity(
                company_id=cid, user_id=user.id,
                activity_type="status_change",
                content=f"[Bulk] Status set to {req.status}",
            ))

    elif req.action == "enrich":
        # Fire enrich for each — best-effort, errors don't block other rows.
        # Synchronous for predictable resource use; if you bulk-enrich 100 it
        # WILL take a minute. Future improvement: queue + background workers.
        companies = (await db.execute(select(Company).where(Company.id.in_(req.company_ids)))).scalars().all()
        for c in companies:
            try:
                await enrich_company(c.id, db=db, user=user)
                affected += 1
            except Exception as e:
                errors.append(f"#{c.id}: {str(e)[:80]}")

    elif req.action == "delete":
        # Cascading delete — Company has cascade='all, delete-orphan' on contacts,
        # deals, activities, tasks. company_tags FK cascades on the join.
        # Audit + webhook fire BEFORE delete so we still have the row data.
        for cid in req.company_ids:
            row = (await db.execute(select(Company).where(Company.id == cid))).scalar_one_or_none()
            if row:
                deleted_snapshot = {
                    "id": row.id, "name": row.name, "website": row.website,
                    "domain": row.domain, "city": row.city, "state": row.state,
                }
                try:
                    from app.services.audit_log import record_audit
                    await record_audit(
                        db, actor=user, action="company.deleted",
                        target_type="company", target_id=row.id, target_label=row.name,
                        metadata=deleted_snapshot,
                    )
                except Exception:
                    pass
                try:
                    from app.services.webhook_dispatch import dispatch_event
                    await dispatch_event(db, "company.deleted", deleted_snapshot)
                except Exception:
                    pass
                await db.delete(row)
                affected += 1

    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {req.action}")

    await db.commit()
    return {
        "action": req.action,
        "affected": affected,
        "requested": len(req.company_ids),
        "errors": errors,
    }
