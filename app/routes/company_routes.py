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
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db
from app.models import User, Company, Contact, Deal, GeneratedEmail, Activity, Task, Tag, company_tags
from app.auth import get_current_user
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
    sort_by: str = "reviews",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(Company)
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

    if sort_by == "reviews":
        query = query.order_by(Company.review_count.desc().nullslast())
    elif sort_by == "rating":
        query = query.order_by(Company.rating.desc().nullslast())
    elif sort_by == "name":
        query = query.order_by(Company.name.asc())
    else:
        query = query.order_by(Company.created_at.desc())

    result = await db.execute(query)
    companies = result.scalars().all()
    return [_company_summary(c) for c in companies]


@router.get("/{company_id}/full")
async def get_company_full(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Full company record: contacts (with their email sequences), deals, activities, tags."""
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    # Contacts with their emails
    contacts_result = await db.execute(
        select(Contact).where(Contact.company_id == company_id).order_by(Contact.is_primary.desc(), Contact.id)
    )
    contacts = contacts_result.scalars().all()

    contacts_data = []
    for c in contacts:
        emails_result = await db.execute(
            select(GeneratedEmail)
            .where(GeneratedEmail.contact_id == c.id)
            .order_by(GeneratedEmail.sequence_order)
        )
        emails = emails_result.scalars().all()
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
            "emails": [_email_to_dict(e) for e in emails],
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
        "enriched": company.enriched,
        "enrichment_summary": company.enrichment_summary,
        "problems_found": problems,
        "problem_count": len(problems),
        "tech_stack": json.loads(company.tech_stack) if company.tech_stack else [],
        "linkedin_url": company.linkedin_url,
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
            }
            for a in activities
        ],
        "created_at": company.created_at.isoformat() if company.created_at else None,
    }


# ============================================================
# Status updates
# ============================================================

class UpdateStatusRequest(BaseModel):
    status: str


@router.patch("/{company_id}/status")
async def update_company_status(
    company_id: int,
    req: UpdateStatusRequest,
    db: AsyncSession = Depends(get_db),
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
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Crawl website, log marketing problems, look up contacts via Apollo/Hunter."""
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if not company.website:
        raise HTTPException(status_code=400, detail="Company has no website to analyze")

    # Website analysis
    analysis = await analyze_website(company.website)
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

    # Netrows + Hunter — import everything they find
    netrows_data, hunter_data = None, None
    netrows_added, netrows_found = 0, 0
    hunter_added, hunter_found = 0, 0

    # Netrows decision-maker first — verified owner emails for SMB (10 credits/call)
    if await get_netrows_api_key(db):
        try:
            nr = await netrows_find_decision_makers(company.website, await get_netrows_api_key(db))
            netrows_data = {
                "decision_makers": [{
                    "email": dm.email, "full_name": dm.full_name,
                    "job_title": dm.job_title, "linkedin_url": dm.linkedin_url,
                    "email_status": dm.email_status, "category": dm.category,
                } for dm in nr.decision_makers],
                "generic_emails": nr.generic_emails,
                "error": nr.error,
            }
            netrows_found = len(nr.decision_makers)
            for dm in nr.decision_makers:
                if await _ensure_contact(db, company_id, dm.full_name, dm.email,
                                         dm.job_title, None, dm.linkedin_url):
                    netrows_added += 1
        except Exception as e:
            netrows_data = {"error": str(e)[:200]}

    if settings.hunter_api_key:
        try:
            hunter = await hunter_search(company.website, settings.hunter_api_key)
            hunter_data = {
                "organization": hunter.organization,
                "emails_found": hunter.emails_found,
                "pattern": hunter.pattern,
                "contacts": [{"email": c.email,
                              "name": f"{c.first_name or ''} {c.last_name or ''}".strip(),
                              "position": c.position, "confidence": c.confidence, "type": c.type}
                             for c in hunter.contacts],
            }
            hunter_found = len(hunter.contacts)
            # Import every contact Hunter returns (deduped by email via _ensure_contact)
            for hc in hunter.contacts:
                if not hc.email:
                    continue
                full = f"{hc.first_name or ''} {hc.last_name or ''}".strip()
                if await _ensure_contact(db, company_id, full, hc.email, hc.position, None, None):
                    hunter_added += 1
        except Exception as e:
            hunter_data = {"error": str(e)[:200]}

    contacts_added = netrows_added + hunter_added

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
        except Exception:
            pass

    # Company enrichment — employee count, industry
    if await get_netrows_api_key(db):
        try:
            ce = await netrows_company_enrich(company.website, await get_netrows_api_key(db))
            if ce:
                if ce.employee_count:
                    company.employee_count = ce.employee_count
                if ce.industry and not company.business_type:
                    company.industry = ce.industry
                if ce.linkedin_username and not company.linkedin_url:
                    company.linkedin_url = f"https://linkedin.com/company/{ce.linkedin_username}"
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
            f"Netrows found {netrows_found}/added {netrows_added} · "
            f"Hunter found {hunter_found}/added {hunter_added}"
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
        "analysis": analysis_dict,
        "local_seo": seo_data,
        "summary": company.enrichment_summary,
        "netrows": netrows_data,
        "hunter": hunter_data,
    }


# ============================================================
# Pursue flow — auto-creates Contact + Deal + Sequence
# ============================================================

class PursueRequest(BaseModel):
    company_ids: list[int]


SEQUENCE_SCHEDULE = [
    {"order": 1, "type": "cold",             "step_type": "email",    "delay_days": 0,  "label": "Initial outreach"},
    {"order": 2, "type": "linkedin_connect", "step_type": "linkedin", "delay_days": 1,  "label": "LinkedIn connect"},
    {"order": 3, "type": "follow_up_1",      "step_type": "email",    "delay_days": 3,  "label": "Follow-up #1"},
    {"order": 4, "type": "linkedin_message", "step_type": "linkedin", "delay_days": 5,  "label": "LinkedIn message"},
    {"order": 5, "type": "follow_up_2",      "step_type": "email",    "delay_days": 7,  "label": "Follow-up #2"},
    {"order": 6, "type": "breakup",          "step_type": "email",    "delay_days": 14, "label": "Breakup email"},
]


@router.post("/pursue")
async def pursue_companies(
    req: PursueRequest,
    db: AsyncSession = Depends(get_db),
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

        # Step 3: generate sequence
        problems = json.loads(company.problems_found) if company.problems_found else []
        if problems:
            now = datetime.now(timezone.utc)
            company.sequence_started_at = now
            first_subject = None
            emails_created = 0

            for step in SEQUENCE_SCHEDULE:
                try:
                    stype = step.get("step_type", "email")

                    if stype == "linkedin":
                        msg_type = "connect" if "connect" in step["type"] else "message"
                        email_data = await generate_linkedin_message(
                            business_name=company.name,
                            business_type=company.business_type or "home services",
                            problems=problems,
                            contact_name=primary.full_name or None,
                            message_type=msg_type,
                        )
                    elif step["type"] == "cold":
                        email_data = await generate_cold_email(
                            business_name=company.name,
                            business_type=company.business_type or "home services",
                            website=company.website or "",
                            problems=problems,
                            contact_name=primary.full_name or None,
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
                            contact_name=primary.full_name or None,
                        )

                    gen_step = GeneratedEmail(
                        contact_id=primary.id,
                        company_id=company.id,
                        step_type=stype,
                        subject=email_data["subject"],
                        body=email_data["body"],
                        email_type=step["type"],
                        sequence_order=step["order"],
                        send_delay_days=step["delay_days"],
                        scheduled_send_at=now + timedelta(days=step["delay_days"]),
                        problems_referenced=json.dumps(problems[:2]),
                    )
                    db.add(gen_step)
                    await db.flush()

                    # Auto-create BDR task for non-email steps
                    if stype != "email":
                        db.add(Task(
                            company_id=company.id,
                            contact_id=primary.id,
                            user_id=company.assigned_to or user.id,
                            description=f"{stype.title()}: {email_data['subject']}",
                            due_date=now + timedelta(days=step["delay_days"]),
                        ))

                    emails_created += 1
                except Exception:
                    continue

            company.email_generated = True
            company.status = "sequencing"

            # Auto-create Deal so it appears on the kanban
            existing_deal = (await db.execute(
                select(Deal).where(Deal.company_id == company.id,
                                   Deal.stage.in_(("prospecting", "qualified", "proposal", "negotiation")))
            )).scalar_one_or_none()
            if not existing_deal:
                deal = Deal(
                    company_id=company.id,
                    name=f"{company.name} — Initial Deal",
                    stage="prospecting",
                    probability=5,
                    assigned_to=user.id,
                )
                db.add(deal)
                await db.flush()
                db.add(Activity(company_id=company.id, user_id=user.id, activity_type="deal_created",
                                content=f"Deal created in pipeline: {deal.name}"))

            db.add(Activity(company_id=company.id, user_id=user.id, activity_type="sequence_created",
                            content=f"Sequence created for {primary.full_name or primary.email or 'primary contact'} ({emails_created} emails)",
                            metadata_json=json.dumps({"contact_id": primary.id, "emails": emails_created})))
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


def _company_summary(c: Company) -> dict:
    problems = json.loads(c.problems_found) if c.problems_found else []
    return {
        "id": c.id,
        "search_id": c.search_id,
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
        "email_generated": c.email_generated,
        "employee_count": c.employee_count,
        "industry": c.industry,
        "linkedin_url": c.linkedin_url,
        "created_at": c.created_at.isoformat() if c.created_at else None,
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
    }


# ============================================================
# On-demand reviews refresh (Netrows /google-maps/reviews — 1 credit)
# ============================================================

@router.post("/{company_id}/refresh-reviews")
async def refresh_reviews(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if not await get_netrows_api_key(db):
        raise HTTPException(status_code=400, detail="Netrows API key not configured")

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
