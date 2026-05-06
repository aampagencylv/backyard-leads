"""
Auto Pilot Campaign routes.
CRUD for campaigns + execution endpoint that runs one batch of the campaign loop.
"""
from __future__ import annotations
from typing import Optional, List
import json
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from pydantic import BaseModel

from app.database import get_db
from app.models import (
    User, Company, Contact, Deal, Campaign, CampaignLog,
    GeneratedEmail, Activity, campaign_members,
)
from app.auth import get_current_user, require_admin
from app.config import settings
from app.runtime_config import get_netrows_api_key
from app.services.map_scraper import search_businesses
from app.services.website_intel import analyze_website
from app.services.local_seo_intel import analyze_local_seo
from app.services.netrows_enrichment import (
    find_decision_makers as netrows_find_decision_makers,
    enrich_company_by_domain as netrows_company_enrich,
)
from app.services.hunter_enrichment import search_domain as hunter_search
from app.services.email_generator import generate_cold_email, generate_follow_up
import secrets

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


SEQUENCE_SCHEDULE = [
    {"order": 1, "type": "cold", "delay_days": 0},
    {"order": 2, "type": "follow_up_1", "delay_days": 3},
    {"order": 3, "type": "follow_up_2", "delay_days": 7},
    {"order": 4, "type": "breakup", "delay_days": 14},
]


# ============================================================
# CRUD
# ============================================================

class CreateCampaignRequest(BaseModel):
    name: str
    business_types: list
    locations: list
    min_reviews: int = 20
    max_reviews: int = 300
    min_rating: float = 3.5
    max_prospects_per_day: int = 10
    max_ai_visibility_score: int = 40
    min_problems: int = 3
    contact_cooldown_days: int = 90
    mode: str = "moderate"
    member_ids: list = []


@router.get("/")
async def list_campaigns(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Campaign).order_by(Campaign.created_at.desc()))
    campaigns = result.scalars().all()
    out = []
    for c in campaigns:
        # Get member names
        mem_result = await db.execute(
            select(User.id, User.first_name, User.last_name)
            .join(campaign_members, User.id == campaign_members.c.user_id)
            .where(campaign_members.c.campaign_id == c.id)
        )
        members = [{"id": r[0], "name": f"{r[1]} {r[2]}".strip()} for r in mem_result.all()]

        out.append({
            "id": c.id,
            "name": c.name,
            "status": c.status,
            "mode": c.mode,
            "business_types": json.loads(c.business_types),
            "locations": json.loads(c.locations),
            "min_reviews": c.min_reviews,
            "max_reviews": c.max_reviews,
            "min_rating": c.min_rating,
            "max_prospects_per_day": c.max_prospects_per_day,
            "contact_cooldown_days": c.contact_cooldown_days,
            "members": members,
            "total_prospects_found": c.total_prospects_found,
            "total_qualified": c.total_qualified,
            "total_sequences_created": c.total_sequences_created,
            "total_replies": c.total_replies,
            "current_location_index": c.current_location_index,
            "current_business_type_index": c.current_business_type_index,
            "prospects_today": c.prospects_today,
            "last_run_at": c.last_run_at.isoformat() if c.last_run_at else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })
    return out


@router.post("/")
async def create_campaign(
    req: CreateCampaignRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    campaign = Campaign(
        name=req.name,
        created_by=user.id,
        business_types=json.dumps(req.business_types),
        locations=json.dumps(req.locations),
        min_reviews=req.min_reviews,
        max_reviews=req.max_reviews,
        min_rating=req.min_rating,
        max_prospects_per_day=req.max_prospects_per_day,
        max_ai_visibility_score=req.max_ai_visibility_score,
        min_problems=req.min_problems,
        contact_cooldown_days=req.contact_cooldown_days,
        mode=req.mode,
    )
    db.add(campaign)
    await db.flush()

    # Add team members
    member_ids = req.member_ids if req.member_ids else [user.id]
    for uid in member_ids:
        await db.execute(campaign_members.insert().values(campaign_id=campaign.id, user_id=uid))

    await db.commit()
    await db.refresh(campaign)

    return {"id": campaign.id, "name": campaign.name, "status": campaign.status}


@router.post("/{campaign_id}/start")
async def start_campaign(
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status == "running":
        raise HTTPException(status_code=400, detail="Campaign is already running")

    campaign.status = "running"
    await db.commit()
    return {"id": campaign.id, "status": "running"}


@router.post("/{campaign_id}/pause")
async def pause_campaign(
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    campaign.status = "paused"
    await db.commit()
    return {"id": campaign.id, "status": "paused"}


@router.post("/{campaign_id}/stop")
async def stop_campaign(
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Full stop — no searching, no enriching, nothing."""
    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    campaign.status = "paused"
    await db.commit()
    return {"id": campaign.id, "status": "paused"}


@router.get("/{campaign_id}/logs")
async def get_campaign_logs(
    campaign_id: int,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(CampaignLog)
        .where(CampaignLog.campaign_id == campaign_id)
        .order_by(CampaignLog.created_at.desc())
        .limit(limit)
    )
    logs = result.scalars().all()
    return [
        {
            "id": l.id,
            "action": l.action,
            "detail": l.detail,
            "company_id": l.company_id,
            "contact_id": l.contact_id,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        }
        for l in logs
    ]


# ============================================================
# Execution — run one batch of the campaign
# ============================================================

@router.post("/{campaign_id}/run-batch")
async def run_campaign_batch(
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Execute one batch of the Auto Pilot campaign.
    Searches one business_type + location combo, enriches, qualifies, creates sequences.
    Call this repeatedly (from UI or cron) until campaign completes.
    """
    return await _execute_batch(campaign_id, db, user)


async def _execute_batch(campaign_id: int, db: AsyncSession, user: User):
    """Core batch execution logic shared by UI and cron endpoints."""
    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status != "running":
        raise HTTPException(status_code=400, detail=f"Campaign is {campaign.status}, not running")

    business_types = json.loads(campaign.business_types)
    locations = json.loads(campaign.locations)

    # Reset daily counter if needed
    now = datetime.now(timezone.utc)
    if not campaign.last_daily_reset or (now - campaign.last_daily_reset).days >= 1:
        campaign.prospects_today = 0
        campaign.last_daily_reset = now

    # Check daily cap
    if campaign.prospects_today >= campaign.max_prospects_per_day:
        return {"status": "daily_cap_reached", "prospects_today": campaign.prospects_today}

    # Check if campaign is done (all combos searched)
    loc_idx = campaign.current_location_index
    bt_idx = campaign.current_business_type_index

    if loc_idx >= len(locations):
        campaign.status = "completed"
        await db.commit()
        return {"status": "completed", "message": "All locations and business types have been searched"}

    location = locations[loc_idx]
    business_type = business_types[bt_idx]

    _log(db, campaign.id, "searched", f"Searching: {business_type} in {location}")

    # Get round-robin team members
    mem_result = await db.execute(
        select(User)
        .join(campaign_members, User.id == campaign_members.c.user_id)
        .where(campaign_members.c.campaign_id == campaign.id)
    )
    team = mem_result.scalars().all()
    if not team:
        raise HTTPException(status_code=400, detail="Campaign has no team members assigned")

    batch_results = {
        "location": location,
        "business_type": business_type,
        "searched": 0,
        "new_companies": 0,
        "enriched": 0,
        "qualified": 0,
        "sequences_created": 0,
        "skipped_dedup": 0,
        "skipped_no_contact": 0,
        "skipped_not_qualified": 0,
    }

    # Step 1: Search Google Maps
    if not settings.google_maps_api_key:
        raise HTTPException(status_code=500, detail="Google Maps API key not configured")

    try:
        businesses = await search_businesses(
            keyword=business_type,
            location=location,
            api_key=settings.google_maps_api_key,
            max_results=40,
        )
    except Exception as e:
        _log(db, campaign.id, "error", f"Search failed: {str(e)[:100]}")
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)[:100]}")

    batch_results["searched"] = len(businesses)
    campaign.total_locations_searched += 1

    remaining_today = campaign.max_prospects_per_day - campaign.prospects_today

    for biz in businesses:
        if remaining_today <= 0:
            break

        # Filter by review count and rating
        rc = biz.review_count or 0
        rating = biz.rating or 0
        if rc < campaign.min_reviews or rc > campaign.max_reviews:
            continue
        if rating < campaign.min_rating:
            continue
        if campaign.must_have_website and not biz.website:
            continue

        # Dedup — check if this business already exists
        existing = await _find_existing_company(db, biz.name, biz.website, biz.phone)
        if existing:
            # Check cooldown
            if existing.status != "new":
                last_contact = existing.updated_at or existing.created_at
                if last_contact and (now - last_contact).days < campaign.contact_cooldown_days:
                    batch_results["skipped_dedup"] += 1
                    continue
            # Check if assigned to another team member not in this campaign
            team_ids = [m.id for m in team]
            if existing.assigned_to and existing.assigned_to not in team_ids:
                batch_results["skipped_dedup"] += 1
                continue
            company = existing
        else:
            company = Company(
                name=biz.name,
                phone=biz.phone,
                website=biz.website,
                address=biz.address,
                city=biz.city,
                state=biz.state,
                rating=biz.rating,
                review_count=biz.review_count,
                business_type=business_type,
            )
            db.add(company)
            await db.flush()
            batch_results["new_companies"] += 1
            campaign.total_prospects_found += 1

        # Step 2: Enrich if not already
        if not company.enriched and company.website:
            try:
                analysis = await analyze_website(company.website)
                company.enriched = True
                company.has_blog = analysis.has_blog
                company.has_social_links = analysis.has_social_links
                company.has_ssl = analysis.has_ssl
                company.site_speed_score = analysis.load_time_seconds
                company.tech_stack = json.dumps(analysis.tech_stack)
                company.problems_found = json.dumps(analysis.problems)
                company.enrichment_summary = _summarize_problems(analysis)

                # Company enrichment
                nr_key = await get_netrows_api_key(db)
                if nr_key:
                    try:
                        ce = await netrows_company_enrich(company.website, nr_key)
                        if ce and ce.employee_count:
                            company.employee_count = ce.employee_count
                        if ce and ce.industry:
                            company.industry = ce.industry
                    except Exception:
                        pass

                # Local SEO + AI visibility
                try:
                    seo = await analyze_local_seo(company.website, company.name, business_type)
                    existing_probs = json.loads(company.problems_found) if company.problems_found else []
                    for f in seo.findings:
                        existing_probs.append({
                            "type": f"seo_{f['issue'].lower().replace(' ', '_')[:30]}",
                            "severity": f["category"],
                            "detail": f["detail"],
                            "angle": f["talking_point"],
                        })
                    company.problems_found = json.dumps(existing_probs)
                    company.enrichment_summary = (company.enrichment_summary or "") + f" Local SEO: {seo.score}/100 | AI Visibility: {seo.ai_visibility_score}/100."
                except Exception:
                    pass

                # Contact lookup — Netrows then Hunter
                nr_key = await get_netrows_api_key(db)
                if nr_key:
                    try:
                        nr = await netrows_find_decision_makers(company.website, nr_key)
                        for dm in nr.decision_makers:
                            await _ensure_contact(db, company.id, dm.full_name, dm.email, dm.job_title, None, dm.linkedin_url)
                    except Exception:
                        pass

                if settings.hunter_api_key:
                    try:
                        hunter = await hunter_search(company.website, settings.hunter_api_key)
                        for hc in hunter.contacts:
                            if hc.email:
                                full = f"{hc.first_name or ''} {hc.last_name or ''}".strip()
                                await _ensure_contact(db, company.id, full, hc.email, hc.position, None, None)
                    except Exception:
                        pass

                await db.commit()
                batch_results["enriched"] += 1
                _log(db, campaign.id, "enriched", f"Enriched: {company.name}", company_id=company.id)
            except Exception as e:
                _log(db, campaign.id, "error", f"Enrich failed for {company.name}: {str(e)[:80]}", company_id=company.id)
                continue

        # Step 3: Qualify
        problems = json.loads(company.problems_found) if company.problems_found else []
        if len(problems) < campaign.min_problems:
            batch_results["skipped_not_qualified"] += 1
            _log(db, campaign.id, "skipped", f"Not qualified: {company.name} ({len(problems)} problems, need {campaign.min_problems})", company_id=company.id)
            continue

        # Find primary contact with email
        contacts_result = await db.execute(
            select(Contact).where(Contact.company_id == company.id, Contact.email.isnot(None), Contact.email != "")
            .order_by(Contact.is_primary.desc())
        )
        primary_contact = contacts_result.scalars().first()

        if campaign.contact_required and not primary_contact:
            batch_results["skipped_no_contact"] += 1
            _log(db, campaign.id, "skipped", f"No contact email: {company.name}", company_id=company.id)
            continue

        # Qualified!
        campaign.total_qualified += 1
        batch_results["qualified"] += 1

        # Round-robin assignment
        assign_idx = campaign.last_assigned_index % len(team)
        assigned_user = team[assign_idx]
        campaign.last_assigned_index = assign_idx + 1
        company.assigned_to = assigned_user.id
        company.status = "pursuing"

        # Create deal if none exists
        existing_deal = await db.execute(
            select(Deal).where(Deal.company_id == company.id)
        )
        if not existing_deal.scalars().first():
            deal = Deal(
                company_id=company.id,
                name=f"{company.name} — {business_type}",
                stage="prospecting",
                probability=5,
                assigned_to=assigned_user.id,
            )
            db.add(deal)

        # Step 4: Generate sequence for primary contact
        if primary_contact:
            existing_emails = await db.execute(
                select(GeneratedEmail).where(GeneratedEmail.contact_id == primary_contact.id)
            )
            if not existing_emails.scalars().first():
                try:
                    first_subject = None
                    emails_created = 0
                    seq_now = datetime.now(timezone.utc)

                    for step in SEQUENCE_SCHEDULE:
                        if step["order"] == 1:
                            email_data = await generate_cold_email(
                                business_name=company.name,
                                business_type=business_type,
                                website=company.website or "",
                                problems=problems,
                                contact_name=primary_contact.full_name,
                                location=f"{company.city}, {company.state}" if company.city else None,
                            )
                            first_subject = email_data["subject"]
                        else:
                            email_data = await generate_follow_up(
                                business_name=company.name,
                                business_type=business_type,
                                problems=problems,
                                previous_email_subject=first_subject or company.name,
                                follow_up_number=step["order"] - 1,
                                contact_name=primary_contact.full_name,
                            )

                        gen_email = GeneratedEmail(
                            company_id=company.id,
                            contact_id=primary_contact.id,
                            subject=email_data["subject"],
                            body=email_data["body"],
                            email_type=step["type"],
                            sequence_order=step["order"],
                            send_delay_days=step["delay_days"],
                            scheduled_send_at=seq_now + timedelta(days=step["delay_days"]),
                            problems_referenced=json.dumps(problems[:2]),
                        )
                        db.add(gen_email)
                        await db.flush()
                        emails_created += 1

                    company.email_generated = True
                    company.status = "sequencing"
                    campaign.total_sequences_created += 1
                    batch_results["sequences_created"] += 1

                    db.add(Activity(
                        company_id=company.id, contact_id=primary_contact.id,
                        user_id=assigned_user.id, activity_type="sequence_created",
                        content=f"Auto Pilot: Sequence created for {primary_contact.full_name} ({emails_created} emails) — Campaign: {campaign.name}",
                    ))

                    _log(db, campaign.id, "sequence_created",
                         f"Sequence for {primary_contact.full_name} at {company.name} (assigned to {assigned_user.full_name})",
                         company_id=company.id, contact_id=primary_contact.id)
                except Exception as e:
                    _log(db, campaign.id, "error", f"Sequence generation failed: {str(e)[:80]}", company_id=company.id)

        campaign.prospects_today += 1
        remaining_today -= 1
        await db.commit()

    # Advance to next business_type/location combo
    bt_idx += 1
    if bt_idx >= len(business_types):
        bt_idx = 0
        loc_idx += 1
    campaign.current_business_type_index = bt_idx
    campaign.current_location_index = loc_idx
    campaign.last_run_at = now

    if loc_idx >= len(locations):
        campaign.status = "completed"
        _log(db, campaign.id, "completed", "Campaign completed — all locations searched")

    await db.commit()

    batch_results["status"] = campaign.status
    batch_results["prospects_today"] = campaign.prospects_today
    return batch_results


# ============================================================
# ============================================================
# Internal cron endpoint — no auth, localhost only
# ============================================================

from fastapi import Request

@router.post("/{campaign_id}/run-batch-internal")
async def run_campaign_batch_internal(
    campaign_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Same as run-batch but without auth. Only accessible from localhost (cron).
    """
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="Internal endpoint — localhost only")

    # Get the campaign creator to use as the acting user
    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status != "running":
        return {"status": campaign.status, "message": f"Campaign is {campaign.status}"}

    creator = (await db.execute(select(User).where(User.id == campaign.created_by))).scalar_one_or_none()
    if not creator:
        raise HTTPException(status_code=500, detail="Campaign creator not found")

    # Fake the request as the creator and delegate to the main function
    # We import and call run_campaign_batch's logic directly
    return await _execute_batch(campaign_id, db, creator)


# ============================================================
# Helpers
# ============================================================

def _log(db, campaign_id, action, detail, company_id=None, contact_id=None):
    db.add(CampaignLog(
        campaign_id=campaign_id,
        action=action,
        detail=detail,
        company_id=company_id,
        contact_id=contact_id,
    ))


async def _find_existing_company(db, name, website, phone):
    """Check if a company already exists by website domain, name+city, or phone."""
    if website:
        domain = website.replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")
        result = await db.execute(
            select(Company).where(Company.website.ilike(f"%{domain}%"))
        )
        existing = result.scalars().first()
        if existing:
            return existing

    if phone:
        result = await db.execute(select(Company).where(Company.phone == phone))
        existing = result.scalars().first()
        if existing:
            return existing

    return None


async def _ensure_contact(db, company_id, name, email, title, phone, linkedin_url):
    """Create a contact if one with this email doesn't already exist at this company."""
    if not email:
        return False
    existing = await db.execute(
        select(Contact).where(Contact.company_id == company_id, Contact.email == email)
    )
    if existing.scalars().first():
        return False

    parts = (name or "").strip().split(maxsplit=1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""

    # Check if this is the first contact for the company
    count_result = await db.execute(
        select(func.count()).select_from(Contact).where(Contact.company_id == company_id)
    )
    is_first = count_result.scalar() == 0

    contact = Contact(
        company_id=company_id,
        first_name=first,
        last_name=last,
        title=title,
        email=email,
        phone=phone,
        linkedin_url=linkedin_url,
        is_primary=is_first,
        unsubscribe_token=secrets.token_urlsafe(32),
    )
    db.add(contact)
    await db.flush()
    return True


def _summarize_problems(analysis):
    problems = analysis.problems
    if not problems:
        return "No major issues found."
    critical = [p for p in problems if p["severity"] == "critical"]
    high = [p for p in problems if p["severity"] == "high"]
    medium = [p for p in problems if p["severity"] == "medium"]
    parts = []
    if critical:
        parts.append(f"{len(critical)} critical")
    if high:
        parts.append(f"{len(high)} high-priority")
    if medium:
        parts.append(f"{len(medium)} improvement opportunities")
    return f"Found {', '.join(parts)}. Top: {problems[0]['detail'][:80]}" if parts else ""
