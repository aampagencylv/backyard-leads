from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel
from app.database import get_db
from app.models import User, Lead, GeneratedEmail
from app.auth import get_current_user
from app.services.website_intel import analyze_website, analysis_to_dict
from app.services.email_generator import generate_cold_email, generate_follow_up
from app.services.apollo_enrichment import enrich_from_domain
from app.services.hunter_enrichment import search_domain as hunter_search
from app.services.local_seo_intel import analyze_local_seo, local_seo_to_dict
from app.config import settings
from datetime import datetime, timezone, timedelta
import json

router = APIRouter(prefix="/api/leads", tags=["leads"])


@router.get("/")
async def list_leads(
    search_id: Optional[int] = None,
    status: Optional[str] = None,
    enriched_only: bool = False,
    min_reviews: Optional[int] = None,
    min_rating: Optional[float] = None,
    sort_by: str = "reviews",  # reviews, rating, created_at, name
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List leads with optional filters. Defaults to sorting by review count (biggest businesses first)."""
    query = select(Lead)

    if search_id:
        query = query.where(Lead.search_id == search_id)
    if status:
        query = query.where(Lead.status == status)
    if enriched_only:
        query = query.where(Lead.enriched == True)
    if min_reviews:
        query = query.where(Lead.review_count >= min_reviews)
    if min_rating:
        query = query.where(Lead.rating >= min_rating)

    # Sort: biggest businesses first by default
    if sort_by == "reviews":
        query = query.order_by(Lead.review_count.desc().nullslast())
    elif sort_by == "rating":
        query = query.order_by(Lead.rating.desc().nullslast())
    elif sort_by == "name":
        query = query.order_by(Lead.business_name.asc())
    else:
        query = query.order_by(Lead.created_at.desc())

    result = await db.execute(query)
    leads = result.scalars().all()

    return [_lead_to_dict(lead) for lead in leads]


@router.get("/{lead_id}")
async def get_lead(
    lead_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get detailed lead info including enrichment data."""
    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead_dict = _lead_to_dict(lead)

    # Include generated emails
    email_result = await db.execute(
        select(GeneratedEmail).where(GeneratedEmail.lead_id == lead_id)
    )
    emails = email_result.scalars().all()
    lead_dict["emails"] = [
        {
            "id": e.id,
            "subject": e.subject,
            "body": e.body,
            "email_type": e.email_type,
            "sequence_order": e.sequence_order,
            "send_delay_days": e.send_delay_days,
            "is_sent": e.is_sent,
            "sent_at": e.sent_at.isoformat() if e.sent_at else None,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in emails
    ]

    return lead_dict


@router.post("/{lead_id}/enrich")
async def enrich_lead(
    lead_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Crawl the lead's website and identify marketing problems."""
    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    if not lead.website:
        raise HTTPException(status_code=400, detail="Lead has no website to analyze")

    # Run website analysis
    analysis = await analyze_website(lead.website)
    analysis_dict = analysis_to_dict(analysis)

    # Update lead with enrichment data
    lead.enriched = True
    lead.has_blog = analysis.has_blog
    lead.has_social_links = analysis.has_social_links
    lead.has_ssl = analysis.has_ssl
    lead.site_speed_score = analysis.load_time_seconds
    lead.mobile_friendly = analysis.mobile_friendly
    lead.tech_stack = json.dumps(analysis.tech_stack)
    lead.problems_found = json.dumps(analysis.problems)
    lead.enrichment_summary = _generate_summary(analysis)

    # Contact enrichment — try Apollo first, then Hunter as fallback
    apollo_data = None
    hunter_data = None
    enrichment_source = None

    # Try Apollo first
    if settings.apollo_api_key:
        try:
            apollo = await enrich_from_domain(lead.website, settings.apollo_api_key)
            if apollo.contacts:
                best_contact = apollo.contacts[0]
                lead.contact_name = best_contact.name
                lead.contact_email = best_contact.email
                lead.contact_title = best_contact.title
                enrichment_source = "apollo"
            apollo_data = {
                "company_name": apollo.company_name,
                "industry": apollo.industry,
                "employee_count": apollo.employee_count,
                "contacts": [
                    {
                        "name": c.name,
                        "title": c.title,
                        "email": c.email,
                        "phone": c.phone,
                        "linkedin": c.linkedin_url,
                    }
                    for c in apollo.contacts
                ],
            }
        except Exception:
            pass

    # Fallback to Hunter if Apollo found no email
    if not lead.contact_email and settings.hunter_api_key:
        try:
            hunter = await hunter_search(lead.website, settings.hunter_api_key)
            hunter_data = {
                "organization": hunter.organization,
                "emails_found": hunter.emails_found,
                "pattern": hunter.pattern,
                "contacts": [
                    {
                        "email": c.email,
                        "name": f"{c.first_name or ''} {c.last_name or ''}".strip(),
                        "position": c.position,
                        "confidence": c.confidence,
                        "type": c.type,
                    }
                    for c in hunter.contacts
                ],
            }
            if hunter.contacts:
                best = hunter.contacts[0]  # Already sorted by personal-first + confidence
                lead.contact_email = best.email
                name = f"{best.first_name or ''} {best.last_name or ''}".strip()
                if name and not lead.contact_name:
                    lead.contact_name = name
                if best.position and not lead.contact_title:
                    lead.contact_title = best.position
                enrichment_source = "hunter"
        except Exception:
            pass

    # Local SEO analysis
    seo_data = None
    try:
        seo_analysis = await analyze_local_seo(
            lead.website,
            business_name=lead.business_name,
            business_type_hint=lead.business_type or "home_services",
        )
        seo_data = local_seo_to_dict(seo_analysis)

        # Merge SEO findings into problems list
        existing_problems = json.loads(lead.problems_found) if lead.problems_found else []
        for finding in seo_analysis.findings:
            existing_problems.append({
                "type": f"seo_{finding['issue'].lower().replace(' ', '_')[:30]}",
                "severity": finding["category"],
                "detail": finding["detail"],
                "angle": finding["talking_point"],
            })
        lead.problems_found = json.dumps(existing_problems)

        # Update summary with SEO score
        lead.enrichment_summary = (lead.enrichment_summary or "") + f" Local SEO Score: {seo_analysis.score}/100."
    except Exception:
        pass  # SEO failure shouldn't block enrichment

    await db.commit()
    await db.refresh(lead)

    return {
        "lead_id": lead.id,
        "business_name": lead.business_name,
        "problems_found": len(json.loads(lead.problems_found) if lead.problems_found else []),
        "analysis": analysis_dict,
        "local_seo": seo_data,
        "summary": lead.enrichment_summary,
        "contact": {
            "name": lead.contact_name,
            "email": lead.contact_email,
            "title": lead.contact_title,
        },
        "enrichment_source": enrichment_source,
        "apollo": apollo_data,
        "hunter": hunter_data,
    }


@router.post("/{lead_id}/enrich-batch")
async def enrich_batch(
    search_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Enrich all leads from a search that have websites."""
    result = await db.execute(
        select(Lead).where(
            Lead.search_id == search_id,
            Lead.website.isnot(None),
            Lead.enriched == False,
        )
    )
    leads = result.scalars().all()

    enriched_count = 0
    for lead in leads:
        try:
            analysis = await analyze_website(lead.website)
            lead.enriched = True
            lead.has_blog = analysis.has_blog
            lead.has_social_links = analysis.has_social_links
            lead.has_ssl = analysis.has_ssl
            lead.site_speed_score = analysis.load_time_seconds
            lead.tech_stack = json.dumps(analysis.tech_stack)
            lead.problems_found = json.dumps(analysis.problems)
            lead.enrichment_summary = _generate_summary(analysis)
            enriched_count += 1
        except Exception:
            continue

    await db.commit()
    return {"enriched": enriched_count, "total": len(leads)}


@router.post("/{lead_id}/generate-email")
async def generate_email(
    lead_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate a personalized cold email for this lead."""
    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    if not lead.enriched or not lead.problems_found:
        raise HTTPException(
            status_code=400,
            detail="Lead must be enriched before generating email. Call /enrich first.",
        )

    problems = json.loads(lead.problems_found)
    if not problems:
        raise HTTPException(status_code=400, detail="No problems found to reference in email")

    # Generate the email
    email_data = await generate_cold_email(
        business_name=lead.business_name,
        business_type=lead.business_type or "home services",
        website=lead.website or "",
        problems=problems,
        contact_name=lead.contact_name,
        location=f"{lead.city}, {lead.state}" if lead.city else None,
    )

    # Save to database
    generated_email = GeneratedEmail(
        lead_id=lead.id,
        subject=email_data["subject"],
        body=email_data["body"],
        email_type="cold",
        problems_referenced=json.dumps(problems[:2]),
    )
    db.add(generated_email)
    lead.email_generated = True
    await db.commit()
    await db.refresh(generated_email)

    return {
        "email_id": generated_email.id,
        "subject": generated_email.subject,
        "body": generated_email.body,
        "problems_used": problems[:2],
    }


@router.post("/{lead_id}/generate-followup")
async def generate_followup(
    lead_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate a follow-up email for a lead that hasn't responded."""
    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    # Get existing emails for this lead
    email_result = await db.execute(
        select(GeneratedEmail)
        .where(GeneratedEmail.lead_id == lead_id)
        .order_by(GeneratedEmail.created_at)
    )
    existing_emails = email_result.scalars().all()

    if not existing_emails:
        raise HTTPException(status_code=400, detail="No initial email found. Generate a cold email first.")

    follow_up_number = len(existing_emails)  # 1st follow-up if 1 email exists, etc.
    problems = json.loads(lead.problems_found) if lead.problems_found else []

    email_data = await generate_follow_up(
        business_name=lead.business_name,
        business_type=lead.business_type or "home services",
        problems=problems,
        previous_email_subject=existing_emails[0].subject,
        follow_up_number=follow_up_number,
        contact_name=lead.contact_name,
    )

    email_type = f"follow_up_{follow_up_number}" if follow_up_number <= 2 else "breakup"

    generated_email = GeneratedEmail(
        lead_id=lead.id,
        subject=email_data["subject"],
        body=email_data["body"],
        email_type=email_type,
    )
    db.add(generated_email)
    await db.commit()
    await db.refresh(generated_email)

    return {
        "email_id": generated_email.id,
        "subject": generated_email.subject,
        "body": generated_email.body,
        "email_type": email_type,
    }


@router.patch("/{lead_id}/status")
async def update_lead_status(
    lead_id: int,
    status: str = Query(..., regex="^(new|pursuing|sequencing|contacted|replied|qualified|converted|not_interested)$"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Update lead status."""
    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead.status = status
    await db.commit()
    return {"lead_id": lead.id, "status": lead.status}


class PursueRequest(BaseModel):
    lead_ids: list


SEQUENCE_SCHEDULE = [
    {"order": 1, "type": "cold", "delay_days": 0, "label": "Initial outreach"},
    {"order": 2, "type": "follow_up_1", "delay_days": 3, "label": "Follow-up #1"},
    {"order": 3, "type": "follow_up_2", "delay_days": 7, "label": "Follow-up #2"},
    {"order": 4, "type": "breakup", "delay_days": 14, "label": "Breakup email"},
]


@router.post("/pursue")
async def pursue_leads(
    req: PursueRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Move selected leads to 'pursuing' status, then:
    1. Enrich each lead (website analysis + Apollo contact lookup + local SEO)
    2. Generate a 4-email sequence for each lead
    3. Schedule the emails on a cadence (day 0, 3, 7, 14)
    """
    results = []

    for lead_id in req.lead_ids:
        result = await db.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            continue

        lead.status = "pursuing"
        await db.commit()

        lead_result = {"lead_id": lead.id, "business_name": lead.business_name, "steps": []}

        # Step 1: Enrich if not already enriched
        if not lead.enriched and lead.website:
            try:
                analysis = await analyze_website(lead.website)
                lead.enriched = True
                lead.has_blog = analysis.has_blog
                lead.has_social_links = analysis.has_social_links
                lead.has_ssl = analysis.has_ssl
                lead.site_speed_score = analysis.load_time_seconds
                lead.mobile_friendly = analysis.mobile_friendly
                lead.tech_stack = json.dumps(analysis.tech_stack)
                lead.problems_found = json.dumps(analysis.problems)
                lead.enrichment_summary = _generate_summary(analysis)

                # Apollo contact lookup
                if settings.apollo_api_key:
                    try:
                        apollo = await enrich_from_domain(lead.website, settings.apollo_api_key)
                        if apollo.contacts:
                            best_contact = apollo.contacts[0]
                            lead.contact_name = best_contact.name
                            lead.contact_email = best_contact.email
                            lead.contact_title = best_contact.title
                    except Exception:
                        pass

                # Hunter fallback if Apollo found no email
                if not lead.contact_email and settings.hunter_api_key:
                    try:
                        hunter = await hunter_search(lead.website, settings.hunter_api_key)
                        if hunter.contacts:
                            best = hunter.contacts[0]
                            lead.contact_email = best.email
                            name = f"{best.first_name or ''} {best.last_name or ''}".strip()
                            if name and not lead.contact_name:
                                lead.contact_name = name
                            if best.position and not lead.contact_title:
                                lead.contact_title = best.position
                    except Exception:
                        pass

                # Local SEO
                try:
                    seo_analysis = await analyze_local_seo(
                        lead.website,
                        business_name=lead.business_name,
                        business_type_hint=lead.business_type or "home_services",
                    )
                    existing_problems = json.loads(lead.problems_found) if lead.problems_found else []
                    for finding in seo_analysis.findings:
                        existing_problems.append({
                            "type": f"seo_{finding['issue'].lower().replace(' ', '_')[:30]}",
                            "severity": finding["category"],
                            "detail": finding["detail"],
                            "angle": finding["talking_point"],
                        })
                    lead.problems_found = json.dumps(existing_problems)
                    lead.enrichment_summary = (lead.enrichment_summary or "") + f" Local SEO Score: {seo_analysis.score}/100."
                except Exception:
                    pass

                await db.commit()
                lead_result["steps"].append("enriched")
            except Exception as e:
                lead_result["steps"].append(f"enrichment_failed: {str(e)[:50]}")

        # Step 2: Generate email sequence
        problems = json.loads(lead.problems_found) if lead.problems_found else []
        if problems:
            now = datetime.now(timezone.utc)
            lead.sequence_started_at = now
            first_subject = None
            emails_created = 0

            for step in SEQUENCE_SCHEDULE:
                try:
                    if step["order"] == 1:
                        email_data = await generate_cold_email(
                            business_name=lead.business_name,
                            business_type=lead.business_type or "home services",
                            website=lead.website or "",
                            problems=problems,
                            contact_name=lead.contact_name,
                            location=f"{lead.city}, {lead.state}" if lead.city else None,
                        )
                        first_subject = email_data["subject"]
                    else:
                        email_data = await generate_follow_up(
                            business_name=lead.business_name,
                            business_type=lead.business_type or "home services",
                            problems=problems,
                            previous_email_subject=first_subject or lead.business_name,
                            follow_up_number=step["order"] - 1,
                            contact_name=lead.contact_name,
                        )

                    scheduled_at = now + timedelta(days=step["delay_days"])

                    generated_email = GeneratedEmail(
                        lead_id=lead.id,
                        subject=email_data["subject"],
                        body=email_data["body"],
                        email_type=step["type"],
                        sequence_order=step["order"],
                        send_delay_days=step["delay_days"],
                        scheduled_send_at=scheduled_at,
                        problems_referenced=json.dumps(problems[:2]),
                    )
                    db.add(generated_email)
                    await db.flush()  # Ensure email is saved before next iteration
                    emails_created += 1
                except Exception:
                    continue

            lead.email_generated = True
            lead.status = "sequencing"
            await db.commit()
            lead_result["steps"].append(f"sequence_created ({emails_created} emails)")

        results.append(lead_result)

    return {
        "pursued": len(results),
        "results": results,
    }


def _lead_to_dict(lead: Lead) -> dict:
    problems = json.loads(lead.problems_found) if lead.problems_found else []
    return {
        "id": lead.id,
        "search_id": lead.search_id,
        "business_name": lead.business_name,
        "phone": lead.phone,
        "website": lead.website,
        "address": lead.address,
        "city": lead.city,
        "state": lead.state,
        "rating": lead.rating,
        "review_count": lead.review_count,
        "business_type": lead.business_type,
        "enriched": lead.enriched,
        "problems_found": problems,
        "problem_count": len(problems),
        "enrichment_summary": lead.enrichment_summary,
        "tech_stack": json.loads(lead.tech_stack) if lead.tech_stack else [],
        "has_blog": lead.has_blog,
        "has_social_links": lead.has_social_links,
        "site_speed_score": lead.site_speed_score,
        "status": lead.status,
        "email_generated": lead.email_generated,
        "contact_name": lead.contact_name,
        "contact_email": lead.contact_email,
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
    }


def _generate_summary(analysis) -> str:
    """Generate a quick human-readable summary of findings."""
    problems = analysis.problems
    if not problems:
        return "No major issues found — this business has a solid web presence."

    critical = [p for p in problems if p["severity"] == "critical"]
    high = [p for p in problems if p["severity"] == "high"]
    medium = [p for p in problems if p["severity"] == "medium"]

    parts = []
    if critical:
        parts.append(f"{len(critical)} critical issue(s)")
    if high:
        parts.append(f"{len(high)} high-priority issue(s)")
    if medium:
        parts.append(f"{len(medium)} improvement opportunity(ies)")

    summary = f"Found {', '.join(parts)}. "
    summary += f"Top issue: {problems[0]['detail']}"
    return summary
