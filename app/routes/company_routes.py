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
from app.models import User, Company, Contact, Deal, GeneratedEmail, Activity, Task, Tag, company_tags, CustomFieldDefinition
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
    rep_id: Optional[int] = None,  # Admin filter: show only this rep's companies
    sort_by: str = "reviews",
    db: AsyncSession = Depends(get_db),
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
    db: AsyncSession = Depends(get_db),
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
    db: AsyncSession = Depends(get_db),
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
                        problems = json.loads(company.problems_found) if company.problems_found else []
                        if problems:
                            try:
                                now = datetime.now(timezone.utc)
                                company.sequence_started_at = now
                                first_subject = None

                                for step in SEQUENCE_SCHEDULE:
                                    stype = step.get("step_type", "email")
                                    if stype == "linkedin":
                                        msg_type = "connect" if "connect" in step["type"] else "message"
                                        edata = await generate_linkedin_message(
                                            business_name=company.name,
                                            business_type=company.business_type or "home services",
                                            problems=problems,
                                            contact_name=primary.full_name,
                                            message_type=msg_type,
                                        )
                                    elif step["type"] == "cold":
                                        edata = await generate_cold_email(
                                            business_name=company.name,
                                            business_type=company.business_type or "home services",
                                            website=company.website or "",
                                            problems=problems,
                                            contact_name=primary.full_name,
                                            location=f"{company.city}, {company.state}" if company.city else None,
                                        )
                                        first_subject = edata["subject"]
                                    else:
                                        edata = await generate_follow_up(
                                            business_name=company.name,
                                            business_type=company.business_type or "home services",
                                            problems=problems,
                                            previous_email_subject=first_subject or company.name,
                                            follow_up_number=step["order"] - 1,
                                            contact_name=primary.full_name,
                                        )

                                    db.add(GeneratedEmail(
                                        contact_id=primary.id, company_id=company.id,
                                        step_type=stype, subject=edata["subject"], body=edata["body"],
                                        email_type=step["type"], sequence_order=step["order"],
                                        send_delay_days=step["delay_days"],
                                        scheduled_send_at=now + timedelta(days=step["delay_days"]),
                                    ))

                                    if stype != "email":
                                        db.add(Task(
                                            company_id=company.id, contact_id=primary.id,
                                            user_id=req.assigned_to or user.id,
                                            description=f"{stype.title()}: {edata['subject']}",
                                            due_date=now + timedelta(days=step["delay_days"]),
                                        ))

                                company.email_generated = True
                                company.status = "sequencing"
                                await db.commit()
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
    db: AsyncSession = Depends(get_db),
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
            "do_not_text": bool(c.do_not_text),
            "do_not_text_at": c.do_not_text_at.isoformat() if c.do_not_text_at else None,
            "phone_type": c.phone_type,
            "phone_carrier": c.phone_carrier,
            "phone_type_checked_at": c.phone_type_checked_at.isoformat() if c.phone_type_checked_at else None,
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
                "recording_url": a.recording_url,
                "transcript": a.transcript,
                "call_summary": a.call_summary,
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
        # Apollo / Netrows often have the cleaner mobile number; fall back to phone.
        contact_phone = c.mobile_phone or c.phone or None
        created = await _ensure_contact(
            db, company_id,
            c.full_name, c.email, c.job_title, contact_phone, c.linkedin_url,
        )
        if created:
            actually_added[c.source] = actually_added.get(c.source, 0) + 1

    netrows_added = actually_added["netrows_dm"]
    hunter_added = actually_added["hunter"]
    apollo_added = actually_added["apollo"]

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


SEQUENCE_SCHEDULE = [
    {"order": 1, "type": "cold",             "step_type": "email",    "delay_days": 0,  "label": "Initial outreach"},
    {"order": 2, "type": "linkedin_connect", "step_type": "linkedin", "delay_days": 1,  "label": "LinkedIn connect"},
    {"order": 3, "type": "follow_up_1",      "step_type": "email",    "delay_days": 3,  "label": "Follow-up #1 (with audit report)"},
    {"order": 4, "type": "imessage",         "step_type": "imessage", "delay_days": 4,  "label": "iMessage (with audit link)"},
    {"order": 5, "type": "linkedin_message", "step_type": "linkedin", "delay_days": 5,  "label": "LinkedIn message (with audit link)"},
    {"order": 6, "type": "follow_up_2",      "step_type": "email",    "delay_days": 7,  "label": "Follow-up #2"},
    {"order": 7, "type": "breakup",          "step_type": "email",    "delay_days": 14, "label": "Breakup email"},
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

        # Step 3: generate sequence
        problems = json.loads(company.problems_found) if company.problems_found else []
        if problems:
            now = datetime.now(timezone.utc)
            company.sequence_started_at = now
            first_subject = None
            emails_created = 0

            # Get LinkedIn URL and audit URL for injecting into steps
            contact_linkedin = primary.linkedin_url or ""
            audit_url = None  # populated after audit generation; sequence emails reference it later via update

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
                        # Add LinkedIn profile link for BDR convenience
                        if contact_linkedin:
                            email_data["body"] = email_data["body"].rstrip() + f"\n\n---\nLinkedIn: {contact_linkedin}"
                        # Add audit link to LinkedIn message (not connect request)
                        if msg_type == "message" and audit_url:
                            email_data["body"] = email_data["body"].rstrip() + f"\n\nAudit report to reference: {audit_url}"

                    elif stype == "imessage":
                        # Generate iMessage with audit link
                        from app.services.email_generator import generate_imessage
                        try:
                            email_data = await generate_imessage(
                                business_name=company.name,
                                business_type=company.business_type or "home services",
                                contact_name=primary.full_name or None,
                                problems=problems,
                                intent="after_email",
                            )
                            # Append audit link
                            if audit_url:
                                email_data["body"] = email_data["body"].rstrip() + f"\n\n{audit_url}"
                            email_data["subject"] = email_data.get("subject", f"iMessage to {primary.full_name or 'contact'}")
                        except Exception:
                            email_data = {
                                "subject": f"iMessage to {primary.full_name or 'contact'}",
                                "body": f"Hey{(' ' + primary.first_name) if primary.first_name else ''}, I sent you an email about your online presence — here's what I found: {audit_url}" if audit_url else f"Hey{(' ' + primary.first_name) if primary.first_name else ''}, did you get my email? Would love to show you what I found about your website.",
                            }

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

                    # Set skip conditions and auto_execute based on step type
                    skip_map = {
                        "email": ["no_email", "opted_out"],
                        "imessage": ["no_phone", "opted_out", "landline"],
                        "linkedin": ["no_linkedin"],
                        "call": ["no_phone"],
                    }
                    auto_map = {"email": True, "imessage": True, "linkedin": False, "call": False, "custom": False}

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
                        skip_if_json=json.dumps(skip_map.get(stype, [])),
                        auto_execute=auto_map.get(stype, False),
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
                from app.routes.deal_routes import recommend_package, STAGE_PROBABILITY as DEAL_STAGE_PROB
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

            # Auto-generate AI Findability Audit
            audit_url = None
            try:
                from app.services.audit_report import generate_audit, render_report_html
                from app.models import AuditReportModel
                import secrets as _secrets

                existing_audit = (await db.execute(
                    select(AuditReportModel).where(AuditReportModel.company_id == company.id)
                )).scalar_one_or_none()

                if not existing_audit and company.website:
                    audit = await generate_audit(
                        website=company.website,
                        company_name=company.name,
                        city=company.city or "",
                        state=company.state or "",
                        business_type=company.business_type or "",
                        rating=company.rating or 0,
                        review_count=company.review_count or 0,
                    )
                    token = _secrets.token_urlsafe(16)
                    public_url = settings.public_url.rstrip("/")
                    html = render_report_html(audit, token, public_url)
                    audit_report = AuditReportModel(
                        company_id=company.id,
                        token=token,
                        html_content=html,
                        ai_findability_score=audit.ai_findability_score,
                        content_citability_score=audit.content_citability_score,
                        local_seo_score=audit.local_seo_score,
                        overall_grade=audit.overall_grade,
                        findings_json=json.dumps([{
                            "type": f.get("type", ""), "severity": f.get("severity", "medium"),
                            "detail": f.get("detail", ""), "angle": f.get("angle", ""),
                        } for f in audit.top_findings]),
                    )
                    db.add(audit_report)
                    audit_url = f"{public_url}/report/{token}"
                    outcome["steps"].append("audit_generated")
                elif existing_audit:
                    public_url = settings.public_url.rstrip("/")
                    audit_url = f"{public_url}/report/{existing_audit.token}"
            except Exception:
                pass  # Audit failure shouldn't block the pipeline

            # Inject audit link into email 2 or 3 of the sequence
            if audit_url:
                try:
                    seq_emails = (await db.execute(
                        select(GeneratedEmail).where(
                            GeneratedEmail.contact_id == primary.id,
                            GeneratedEmail.step_type == "email",
                            GeneratedEmail.is_sent == False,
                        ).order_by(GeneratedEmail.sequence_order)
                    )).scalars().all()
                    # Target the 2nd or 3rd email step
                    email_steps = [e for e in seq_emails if e.step_type == "email"]
                    target = email_steps[1] if len(email_steps) > 1 else (email_steps[0] if email_steps else None)
                    if target:
                        audit_line = f"\n\nI actually ran an analysis on {company.name}'s online presence — thought you might find it interesting: {audit_url}"
                        target.body = target.body.rstrip() + audit_line
                except Exception:
                    pass

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


@router.post("/{company_id}/clear-enrichment")
async def clear_company_enrichment(
    company_id: int,
    db: AsyncSession = Depends(get_db),
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
        content=f"Cleared Netrows-derived fields ({len(cleared)}): {', '.join(cleared) or 'none'}",
    ))
    await db.commit()
    return {"cleared_fields": cleared, "message": "Re-enrich now to rebuild from scratch"}


@router.post("/{company_id}/refresh-instagram-posts")
async def refresh_instagram_posts(
    company_id: int,
    db: AsyncSession = Depends(get_db),
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
        raise HTTPException(status_code=400, detail="Netrows API key not configured")

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
    db: AsyncSession = Depends(get_db),
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
        raise HTTPException(status_code=400, detail="Netrows API key not configured")

    from app.services.netrows_enrichment import company_insights as _insights
    from app.services.credit_meter import meter, make_idem_key

    ci = await _insights(company.website or company.domain, nr_key)
    if ci is None:
        return {"found": False, "message": "Insights endpoint returned no data — domain may not be in Netrows' database"}
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
    db: AsyncSession = Depends(get_db),
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
        raise HTTPException(status_code=400, detail="Netrows API key not configured")

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
    db: AsyncSession = Depends(get_db),
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
        raise HTTPException(status_code=400, detail="Netrows API key not configured")

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
    db: AsyncSession = Depends(get_db),
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
    # would violate the (company_id, tag_id) PK. Use INSERT OR IGNORE.
    placeholders = ",".join(f":id{i}" for i in range(len(req.merge_from_ids)))
    params = {"keep": req.keep_id, **{f"id{i}": v for i, v in enumerate(req.merge_from_ids)}}
    await db.execute(
        sql_text(f"""
            INSERT OR IGNORE INTO company_tags (company_id, tag_id)
            SELECT :keep, tag_id FROM company_tags WHERE company_id IN ({placeholders})
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
    db: AsyncSession = Depends(get_db),
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
        # INSERT OR IGNORE — composite PK (company_id, tag_id) auto-dedupes
        await db.execute(
            sql_text(f"""
                INSERT OR IGNORE INTO company_tags (company_id, tag_id)
                SELECT id, :tid FROM companies WHERE id IN ({placeholders})
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
