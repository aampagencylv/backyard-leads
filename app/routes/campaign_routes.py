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
from sqlalchemy import select, func, or_, text
from pydantic import BaseModel

from app.tenancy import get_tenant_db
from app.models import (
    User, Company, Contact, Deal, Campaign, CampaignLog, CampaignTarget, CampaignRun,
    GeneratedEmail, Activity, Task, campaign_members,
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
from app.services.email_generator import generate_cold_email, generate_follow_up, generate_linkedin_message
import secrets

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


async def _sync_campaign_targets(db: AsyncSession, campaign: Campaign) -> None:
    """Reconcile CampaignTarget rows against the campaign's current
    business_types × locations cross product.

    - New (vertical, location) pair → fresh CampaignTarget at status='active'
    - Existing pair still in the cross product → leave as-is (preserves
      cursor + counters across edits)
    - Pair no longer in the cross product → status='paused' with a
      'removed_from_config' reason (kept for history; not deleted so
      the morning brief can still summarize past activity)

    Called from the create endpoint and (when added) any future update
    endpoint that touches business_types/locations.
    """
    business_types = json.loads(campaign.business_types) if campaign.business_types else []
    locations = json.loads(campaign.locations) if campaign.locations else []
    desired_pairs = {(bt, loc) for bt in business_types for loc in locations}

    existing_rows = (await db.execute(
        select(CampaignTarget).where(CampaignTarget.campaign_id == campaign.id)
    )).scalars().all()
    existing_pairs = {(r.vertical, r.location): r for r in existing_rows}

    # Pause pairs that are no longer in the desired set
    for pair, row in existing_pairs.items():
        if pair not in desired_pairs and row.status != "paused":
            row.status = "paused"
            row.paused_reason = "removed_from_config"

    # Create new pairs (or unpause if the user added a previously-removed pair back)
    for pair in desired_pairs:
        existing = existing_pairs.get(pair)
        if existing is None:
            db.add(CampaignTarget(
                campaign_id=campaign.id,
                vertical=pair[0],
                location=pair[1],
            ))
        elif existing.status == "paused" and existing.paused_reason == "removed_from_config":
            existing.status = "active"
            existing.paused_reason = None

    await db.commit()


# Use the same 13-step multi-channel template as the manual sequence.
# Calls use "no_phone_at_all" which checks both contact AND company phone.
# iMessage/SMS use "no_mobile" which only skips when there's no cell number.
from app.services.sequence_engine import DEFAULT_30DAY_TEMPLATE
SEQUENCE_SCHEDULE = DEFAULT_30DAY_TEMPLATE


# ============================================================
# CRUD
# ============================================================

class CreateCampaignRequest(BaseModel):
    name: str
    business_types: list
    locations: list
    expand_metros: bool = False  # Auto-expand cities into metro area suburbs
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
    include_archived: bool = False,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """List campaigns. Archived campaigns are hidden by default; pass
    `?include_archived=true` to see them (useful for an admin-only
    Archive view)."""
    q = select(Campaign).order_by(Campaign.created_at.desc())
    if not include_archived:
        q = q.where(Campaign.archived_at.is_(None))
    result = await db.execute(q)
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
            "archived_at": c.archived_at.isoformat() if c.archived_at else None,
            "scheduled_start_at": c.scheduled_start_at.isoformat() if c.scheduled_start_at else None,
        })
    return out


@router.get("/metro-areas")
async def list_metro_areas(user: User = Depends(get_current_user)):
    """Return available metro area presets for the campaign location picker."""
    from app.services.metro_areas import get_available_metros
    return get_available_metros()


@router.post("/expand-locations")
async def expand_locations(
    locations: list[str],
    user: User = Depends(get_current_user),
):
    """Expand location names into suburb lists using metro area mappings.
    Used by the campaign creation UI to show what cities will be searched."""
    from app.services.metro_areas import expand_metro
    expanded = []
    for loc in locations:
        expanded.extend(expand_metro(loc))
    # Dedupe while preserving order
    seen = set()
    unique = []
    for city in expanded:
        if city.lower() not in seen:
            seen.add(city.lower())
            unique.append(city)
    return {"locations": unique, "count": len(unique)}


@router.post("/")
async def create_campaign(
    req: CreateCampaignRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    # Expand metro areas if requested
    locations = req.locations
    if req.expand_metros:
        from app.services.metro_areas import expand_metro
        expanded = []
        for loc in locations:
            expanded.extend(expand_metro(loc))
        seen = set()
        locations = [c for c in expanded if c.lower() not in seen and not seen.add(c.lower())]

    campaign = Campaign(
        name=req.name,
        created_by=user.id,
        business_types=json.dumps(req.business_types),
        locations=json.dumps(locations),
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

    # God Mode: spawn one CampaignTarget per (vertical, location) pair
    # so the runner can iterate them concurrently with their own cursors.
    await _sync_campaign_targets(db, campaign)

    return {"id": campaign.id, "name": campaign.name, "status": campaign.status}


@router.post("/{campaign_id}/start")
async def start_campaign(
    campaign_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status == "running":
        raise HTTPException(status_code=400, detail="Campaign is already running")

    campaign.status = "running"
    campaign.scheduled_start_at = None  # starting now overrides any pending schedule
    await db.commit()
    return {"id": campaign.id, "status": "running"}


class ScheduleCampaignRequest(BaseModel):
    scheduled_start_at: str  # ISO 8601, UTC (e.g. "2026-06-01T14:00:00Z")


@router.post("/{campaign_id}/schedule")
async def schedule_campaign(
    campaign_id: int,
    req: ScheduleCampaignRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Schedule a campaign to start at a future time. The activation loop
    flips it to 'running' once scheduled_start_at passes."""
    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status == "running":
        raise HTTPException(status_code=400, detail="Campaign is already running — pause it first to reschedule")

    # Parse the incoming ISO timestamp. Accept trailing 'Z'.
    try:
        raw = req.scheduled_start_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid scheduled_start_at — expected ISO 8601")

    if dt <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Scheduled time must be in the future")

    campaign.status = "scheduled"
    campaign.scheduled_start_at = dt
    await db.commit()
    return {"id": campaign.id, "status": "scheduled", "scheduled_start_at": dt.isoformat()}


@router.post("/{campaign_id}/unschedule")
async def unschedule_campaign(
    campaign_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Cancel a pending schedule — returns the campaign to draft."""
    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status != "scheduled":
        raise HTTPException(status_code=400, detail="Campaign is not scheduled")

    campaign.status = "draft"
    campaign.scheduled_start_at = None
    await db.commit()
    return {"id": campaign.id, "status": "draft"}


@router.post("/{campaign_id}/pause")
async def pause_campaign(
    campaign_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    campaign.status = "paused"
    await db.commit()
    return {"id": campaign.id, "status": "paused"}


class UpdateCampaignRequest(BaseModel):
    name: Optional[str] = None
    max_prospects_per_day: Optional[int] = None
    min_reviews: Optional[int] = None
    max_reviews: Optional[int] = None
    min_rating: Optional[float] = None
    must_have_website: Optional[bool] = None
    max_ai_visibility_score: Optional[int] = None
    min_problems: Optional[int] = None
    contact_required: Optional[bool] = None
    contact_cooldown_days: Optional[int] = None
    mode: Optional[str] = None  # moderate | full_auto
    business_types: Optional[List[str]] = None
    locations: Optional[List[str]] = None
    expand_metros: Optional[bool] = None  # If True + locations set → run metro expansion


@router.patch("/{campaign_id}")
async def update_campaign(
    campaign_id: int,
    req: UpdateCampaignRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Edit a campaign — works on running, paused, or draft campaigns.
    Changes take effect on the next batch tick (running campaigns don't
    need to be paused to edit)."""
    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if req.name is not None:
        campaign.name = req.name.strip()[:255]
    if req.max_prospects_per_day is not None:
        campaign.max_prospects_per_day = max(1, min(500, int(req.max_prospects_per_day)))
    if req.min_reviews is not None:
        campaign.min_reviews = max(0, int(req.min_reviews))
    if req.max_reviews is not None:
        campaign.max_reviews = max(0, int(req.max_reviews))
    if req.min_rating is not None:
        campaign.min_rating = max(0, min(5, float(req.min_rating)))
    if req.must_have_website is not None:
        campaign.must_have_website = bool(req.must_have_website)
    if req.max_ai_visibility_score is not None:
        campaign.max_ai_visibility_score = max(0, min(100, int(req.max_ai_visibility_score)))
    if req.min_problems is not None:
        campaign.min_problems = max(0, int(req.min_problems))
    if req.contact_required is not None:
        campaign.contact_required = bool(req.contact_required)
    if req.contact_cooldown_days is not None:
        campaign.contact_cooldown_days = max(0, int(req.contact_cooldown_days))
    if req.mode is not None and req.mode in ("moderate", "full_auto"):
        campaign.mode = req.mode
    if req.business_types is not None and len(req.business_types) > 0:
        campaign.business_types = json.dumps(req.business_types)
    if req.locations is not None and len(req.locations) > 0:
        new_locs = req.locations
        # Honor expand_metros on update the same way create does. Without
        # this, editing a campaign's locations would silently lose suburb
        # expansion even when the UI checkbox is checked — what bit
        # campaigns #9/#10/#11 (Dallas, Naples, Palm Beach).
        if req.expand_metros:
            from app.services.metro_areas import expand_metro
            expanded = []
            for loc in new_locs:
                expanded.extend(expand_metro(loc))
            seen = set()
            new_locs = [c for c in expanded if c.lower() not in seen and not seen.add(c.lower())]
        campaign.locations = json.dumps(new_locs)

    # If a completed campaign is edited, reset it to running so the engine
    # re-scans with the new criteria (wider review range, new locations, etc).
    # Also reset the cursor so it starts from the beginning of each market.
    if campaign.status == "completed":
        campaign.status = "running"
        campaign.current_location_index = 0
        campaign.current_business_type_index = 0
        campaign.prospects_today = 0

    await db.commit()
    await db.refresh(campaign)
    return {
        "id": campaign.id,
        "name": campaign.name,
        "status": campaign.status,
        "max_prospects_per_day": campaign.max_prospects_per_day,
        "mode": campaign.mode,
        "updated": True,
    }


@router.post("/{campaign_id}/stop")
async def stop_campaign(
    campaign_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Full stop — no searching, no enriching, nothing."""
    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    campaign.status = "paused"
    await db.commit()
    return {"id": campaign.id, "status": "paused"}


@router.post("/{campaign_id}/archive")
async def archive_campaign(
    campaign_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Archive a campaign — removes it from the active Auto Pilot list
    but preserves all its data + activity history. Reversible via the
    /unarchive endpoint."""
    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status == "running":
        raise HTTPException(status_code=400, detail="Pause or stop the campaign before archiving")
    campaign.archived_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": campaign.id, "status": campaign.status, "archived": True}


@router.post("/{campaign_id}/unarchive")
async def unarchive_campaign(
    campaign_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Bring a previously-archived campaign back to the active list."""
    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    campaign.archived_at = None
    await db.commit()
    return {"id": campaign.id, "status": campaign.status, "archived": False}


@router.delete("/{campaign_id}")
async def delete_campaign(
    campaign_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Permanently delete a campaign. Only allowed for campaigns that
    NEVER LAUNCHED — i.e. status='draft' with no sequences/prospects
    attributed. Running, paused, or completed campaigns with any
    activity history must be archived instead so audit trail is
    preserved."""
    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status != "draft":
        raise HTTPException(
            status_code=400,
            detail=f"Can only delete draft campaigns. This one is '{campaign.status}' — archive it instead.",
        )
    if (campaign.total_prospects_found or 0) > 0 or (campaign.total_sequences_created or 0) > 0:
        raise HTTPException(
            status_code=400,
            detail="Campaign already has prospects/sequences attributed — archive instead of delete.",
        )
    # Clean up the join + log tables first (no cascading FKs)
    from app.models import CampaignLog, CampaignTarget, CampaignRun
    await db.execute(text("DELETE FROM campaign_members WHERE campaign_id = :c"), {"c": campaign_id})
    await db.execute(select(CampaignLog).where(CampaignLog.campaign_id == campaign_id))
    await db.execute(text("DELETE FROM campaign_logs WHERE campaign_id = :c"), {"c": campaign_id})
    await db.execute(text("DELETE FROM campaign_targets WHERE campaign_id = :c"), {"c": campaign_id})
    await db.execute(text("DELETE FROM campaign_runs WHERE campaign_id = :c"), {"c": campaign_id})
    await db.delete(campaign)
    await db.commit()
    return {"deleted": True, "id": campaign_id}


@router.get("/{campaign_id}/targets")
async def get_campaign_targets(
    campaign_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Per-target portfolio for a campaign — what God Mode is producing
    in each (vertical, location) pair. Drives the Settings UI table."""
    rows = (await db.execute(
        select(CampaignTarget)
        .where(CampaignTarget.campaign_id == campaign_id)
        .order_by(CampaignTarget.status, CampaignTarget.vertical, CampaignTarget.location)
    )).scalars().all()
    return [
        {
            "id": t.id,
            "vertical": t.vertical,
            "location": t.location,
            "weight": t.weight,
            "status": t.status,
            "paused_reason": t.paused_reason,
            "contacts_enrolled": t.contacts_enrolled,
            "enrolled_today": t.enrolled_today,
            "credits_spent": round(t.credits_spent, 4) if t.credits_spent else 0,
            "consecutive_empty_runs": t.consecutive_empty_runs,
            "scrape_cursor": t.scrape_cursor,
            "last_run_at": t.last_run_at.isoformat() if t.last_run_at else None,
        }
        for t in rows
    ]


@router.patch("/{campaign_id}/targets/{target_id}")
async def update_campaign_target(
    campaign_id: int,
    target_id: int,
    req: dict,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Manual control over a single target — pause / resume / re-weight.
    Body fields (all optional): { weight: int, status: 'active'|'paused' }."""
    target = (await db.execute(
        select(CampaignTarget).where(
            CampaignTarget.id == target_id,
            CampaignTarget.campaign_id == campaign_id,
        )
    )).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")

    if "weight" in req and req["weight"] is not None:
        try:
            w = int(req["weight"])
            if 1 <= w <= 10:
                target.weight = w
        except (ValueError, TypeError):
            pass

    if "status" in req and req["status"] in ("active", "paused"):
        target.status = req["status"]
        if target.status == "active":
            # Manual unpause: clear exhaustion counter so it gets fresh runs
            target.consecutive_empty_runs = 0
            target.paused_reason = None
        elif target.status == "paused":
            target.paused_reason = "manual"

    await db.commit()
    return {
        "id": target.id, "vertical": target.vertical, "location": target.location,
        "weight": target.weight, "status": target.status,
        "paused_reason": target.paused_reason,
    }


@router.get("/{campaign_id}/logs")
async def get_campaign_logs(
    campaign_id: int,
    limit: int = 50,
    db: AsyncSession = Depends(get_tenant_db),
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
    db: AsyncSession = Depends(get_tenant_db),
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
    from app.runtime_config import get_google_maps_api_key
    maps_key = await get_google_maps_api_key(db)
    if not maps_key:
        raise HTTPException(status_code=500, detail="Google Maps API key not configured")

    try:
        businesses = await search_businesses(
            keyword=business_type,
            location=location,
            api_key=maps_key,
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
                _su = analysis.social_urls or {}
                if _su.get("facebook") and not company.facebook_url:
                    company.facebook_url = _su["facebook"][:500]
                if _su.get("instagram") and not company.instagram_url:
                    company.instagram_url = _su["instagram"][:500]
                if _su.get("youtube") and not company.youtube_url:
                    company.youtube_url = _su["youtube"][:500]
                if _su.get("tiktok") and not company.tiktok_url:
                    company.tiktok_url = _su["tiktok"][:500]

                # Company enrichment
                nr_key = await get_netrows_api_key(db)
                if nr_key:
                    try:
                        ce = await netrows_company_enrich(company.website, nr_key, expected_name=company.name)
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
                            await _ensure_contact(db, company.id, dm.full_name, dm.email, dm.job_title, company.phone, dm.linkedin_url)
                    except Exception:
                        pass

                if settings.hunter_api_key:
                    try:
                        hunter = await hunter_search(company.website, settings.hunter_api_key)
                        for hc in hunter.contacts:
                            if hc.email:
                                full = f"{hc.first_name or ''} {hc.last_name or ''}".strip()
                                await _ensure_contact(db, company.id, full, hc.email, hc.position, company.phone, None)
                    except Exception:
                        pass

                # Meter the enrichment
                try:
                    from app.services.credit_meter import meter, make_idem_key
                    await meter(db, action_type="enrich_netrows",
                                idempotency_key=make_idem_key("enrich_netrows", company.id, "campaign"),
                                user_id=campaign.created_by, action_ref=f"company:{company.id}",
                                metadata={"via": "campaign", "campaign_id": campaign.id})
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

        # Dedupe across business_type searches — a "pool builder" often
        # shows up again as a "landscaping company" and "deck builder"
        # in Google Maps. Without this check total_qualified would over-
        # count because each search re-passes the criteria gate.
        if company.email_generated:
            batch_results.setdefault("skipped_already_sequenced", 0)
            batch_results["skipped_already_sequenced"] += 1
            _log(db, campaign.id, "skipped", f"Already sequenced (cross-vertical dupe): {company.name}", company_id=company.id)
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
            from app.routes.deal_routes import recommend_package
            pkg = recommend_package(company.employee_count)
            deal = Deal(
                company_id=company.id,
                name=f"{company.name} — {business_type}",
                value=0,
                package=pkg,
                contract_months=6,
                stage="in_sequence",
                probability=0,
                assigned_to=assigned_user.id,
            )
            db.add(deal)

        # Step 4: Enroll primary contact in the engagement engine.
        # Pre-cutover this called start_sequence_from_template which wrote
        # GeneratedEmail rows for the legacy sequence_engine cron to dispatch.
        # The legacy cron is disabled, so we route directly to the new engine
        # via lifecycle.start_engagement — same template, same Claude pre-gen,
        # but writes engagements + actions for the new dispatcher to pick up.
        if primary_contact:
            # Only ACTIVE engagements should block re-enrollment.
            # Terminal engagements (closed_lost, opted-out) must allow
            # restore-from-disqualified to start a fresh enrollment;
            # otherwise the contact gets stuck in 'pursuing' forever.
            existing_eng = (await db.execute(text(
                "SELECT 1 FROM engagements WHERE contact_id = :c AND status = 'active' LIMIT 1"
            ), {"c": primary_contact.id})).first()
            if existing_eng is None and not company.email_generated:
                try:
                    from app.engagement_engine.lifecycle import start_engagement
                    actions_created = await start_engagement(
                        db, primary_contact,
                        template=SEQUENCE_SCHEDULE,
                        sequence_label="main",
                        pre_generate_content=True,
                        assigned_bdr_id=assigned_user.id,
                        initiated_by="autopilot",
                    )
                    campaign.total_sequences_created += 1
                    batch_results["sequences_created"] += 1

                    _log(db, campaign.id, "sequence_created",
                         f"Sequence for {primary_contact.full_name} at {company.name} (assigned to {assigned_user.full_name}) — {actions_created} actions",
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
# God Mode runner — multi-target concurrent execution per tick
# ============================================================
# Replaces the legacy "advance current_location_index by 1 each tick"
# model with one that processes every active CampaignTarget per cron
# tick, allocating the daily prospect cap across targets by weight and
# tracking per-target spend, cursors, and exhaustion.

async def _process_business_through_pipeline(
    db: AsyncSession,
    campaign: Campaign,
    business_type: str,
    biz,
    team: list,
    now: datetime,
) -> str:
    """Take one Maps result through dedup → enrich → qualify → assign →
    sequence-gen. Returns a status string the caller uses to update
    counters. Caller is responsible for committing after the return.

    Returns one of:
      'enrolled', 'skipped_filters', 'skipped_dedup',
      'skipped_not_qualified', 'skipped_no_contact', 'enriched_no_seq',
      'error'
    """
    rc = biz.review_count or 0
    rating = biz.rating or 0
    if rc < campaign.min_reviews or rc > campaign.max_reviews:
        return "skipped_filters"
    if rating < campaign.min_rating:
        return "skipped_filters"
    if campaign.must_have_website and not biz.website:
        return "skipped_filters"

    # Dedup — check if this business already exists in CRM
    existing = await _find_existing_company(db, biz.name, biz.website, biz.phone)
    if existing:
        if existing.status != "new":
            last_contact = existing.updated_at or existing.created_at
            if last_contact and (now - last_contact).days < campaign.contact_cooldown_days:
                return "skipped_dedup"
        team_ids = [m.id for m in team]
        if existing.assigned_to and existing.assigned_to not in team_ids:
            return "skipped_dedup"
        company = existing
    else:
        company = Company(
            name=biz.name, phone=biz.phone, website=biz.website,
            address=biz.address, city=biz.city, state=biz.state,
            rating=biz.rating, review_count=biz.review_count,
            business_type=business_type,
        )
        db.add(company)
        await db.flush()
        campaign.total_prospects_found += 1

    # Enrich if not already
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
            _su = analysis.social_urls or {}
            if _su.get("facebook") and not company.facebook_url:
                company.facebook_url = _su["facebook"][:500]
            if _su.get("instagram") and not company.instagram_url:
                company.instagram_url = _su["instagram"][:500]
            if _su.get("youtube") and not company.youtube_url:
                company.youtube_url = _su["youtube"][:500]
            if _su.get("tiktok") and not company.tiktok_url:
                company.tiktok_url = _su["tiktok"][:500]

            nr_key = await get_netrows_api_key(db)
            if nr_key:
                try:
                    ce = await netrows_company_enrich(company.website, nr_key, expected_name=company.name)
                    if ce and ce.employee_count:
                        company.employee_count = ce.employee_count
                    if ce and ce.industry:
                        company.industry = ce.industry
                except Exception:
                    pass

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

            if nr_key:
                try:
                    nr = await netrows_find_decision_makers(company.website, nr_key)
                    for dm in nr.decision_makers:
                        await _ensure_contact(db, company.id, dm.full_name, dm.email, dm.job_title, company.phone, dm.linkedin_url)
                except Exception:
                    pass

            if settings.hunter_api_key:
                try:
                    hunter = await hunter_search(company.website, settings.hunter_api_key)
                    for hc in hunter.contacts:
                        if hc.email:
                            full = f"{hc.first_name or ''} {hc.last_name or ''}".strip()
                            await _ensure_contact(db, company.id, full, hc.email, hc.position, company.phone, None)
                except Exception:
                    pass

            # Meter the enrichment
            try:
                from app.services.credit_meter import meter, make_idem_key
                await meter(db, action_type="enrich_netrows",
                            idempotency_key=make_idem_key("enrich_netrows", company.id, "godmode"),
                            user_id=campaign.created_by, action_ref=f"company:{company.id}",
                            metadata={"via": "campaign_godmode", "campaign_id": campaign.id})
            except Exception:
                pass

            await db.flush()
        except Exception as e:
            _log(db, campaign.id, "error", f"Enrich failed for {company.name}: {str(e)[:80]}", company_id=company.id)
            return "error"

    # Qualify
    problems = json.loads(company.problems_found) if company.problems_found else []
    if len(problems) < campaign.min_problems:
        _log(db, campaign.id, "skipped",
             f"Not qualified: {company.name} ({len(problems)} problems, need {campaign.min_problems})",
             company_id=company.id)
        return "skipped_not_qualified"

    # Primary contact
    contacts_result = await db.execute(
        select(Contact).where(Contact.company_id == company.id, Contact.email.isnot(None), Contact.email != "")
        .order_by(Contact.is_primary.desc())
    )
    primary_contact = contacts_result.scalars().first()
    if campaign.contact_required and not primary_contact:
        _log(db, campaign.id, "skipped", f"No contact email: {company.name}", company_id=company.id)
        return "skipped_no_contact"

    # Cross-vertical dedupe — same fix as in _execute_batch above.
    if company.email_generated:
        _log(db, campaign.id, "skipped", f"Already sequenced (cross-vertical dupe): {company.name}", company_id=company.id)
        return "skipped_already_sequenced"

    # Round-robin assign
    assign_idx = campaign.last_assigned_index % len(team)
    assigned_user = team[assign_idx]
    campaign.last_assigned_index = assign_idx + 1
    company.assigned_to = assigned_user.id
    company.status = "pursuing"
    campaign.total_qualified += 1

    # Create deal if missing
    existing_deal = await db.execute(select(Deal).where(Deal.company_id == company.id))
    if not existing_deal.scalars().first():
        from app.routes.deal_routes import recommend_package
        pkg = recommend_package(company.employee_count)
        deal = Deal(
            company_id=company.id,
            name=f"{company.name} — {business_type}",
            value=0, package=pkg, contract_months=6,
            stage="in_sequence", probability=0,
            assigned_to=assigned_user.id,
        )
        db.add(deal)

    # Sequence gen — enroll via the engagement engine.
    if not primary_contact:
        return "enriched_no_seq"

    # Only ACTIVE engagements should block re-enrollment — see comment in
    # _execute_batch for why we don't gate on terminal engagements.
    existing_eng = (await db.execute(text(
        "SELECT 1 FROM engagements WHERE contact_id = :c AND status = 'active' LIMIT 1"
    ), {"c": primary_contact.id})).first()
    if existing_eng is not None:
        return "enrolled"  # already enrolled; treat as success without re-genning

    try:
        from app.engagement_engine.lifecycle import start_engagement
        actions_created = await start_engagement(
            db, primary_contact,
            template=SEQUENCE_SCHEDULE,
            sequence_label="main",
            pre_generate_content=True,
            assigned_bdr_id=assigned_user.id,
            initiated_by="autopilot",
        )
        campaign.total_sequences_created += 1
        _log(db, campaign.id, "sequence_created",
             f"Sequence for {primary_contact.full_name} at {company.name} (assigned to {assigned_user.full_name}) — {actions_created} actions",
             company_id=company.id, contact_id=primary_contact.id)
        return "enrolled"
    except Exception as e:
        _log(db, campaign.id, "error", f"Sequence generation failed for {company.name}: {str(e)[:80]}", company_id=company.id)
        return "enriched_no_seq"


async def _execute_god_mode_batch(campaign_id: int, db: AsyncSession) -> dict:
    """God Mode batch execution — processes ALL active CampaignTargets in
    one tick. Replaces the legacy single-pair runner that advanced
    current_location_index one slot at a time.

    Per-target daily allowance is (weight / sum_active_weights) * campaign.max_prospects_per_day.
    Each target keeps its own counters and cursor; campaigns only finish when EVERY target is exhausted.

    Writes a CampaignRun row summarizing the tick — drives the future morning brief.
    """
    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status != "running":
        return {"status": campaign.status, "message": f"Campaign is {campaign.status}"}

    now = datetime.now(timezone.utc)

    # Reset campaign-level daily counter at UTC midnight
    if not campaign.last_daily_reset or (now - campaign.last_daily_reset).days >= 1:
        campaign.prospects_today = 0
        campaign.last_daily_reset = now

    if campaign.prospects_today >= campaign.max_prospects_per_day:
        return {"status": "daily_cap_reached", "prospects_today": campaign.prospects_today}

    # Load active targets. Legacy campaigns may have none yet — sync from
    # the cross product before bailing.
    targets = (await db.execute(
        select(CampaignTarget).where(
            CampaignTarget.campaign_id == campaign.id,
            CampaignTarget.status == "active",
        )
    )).scalars().all()
    if not targets:
        await _sync_campaign_targets(db, campaign)
        targets = (await db.execute(
            select(CampaignTarget).where(
                CampaignTarget.campaign_id == campaign.id,
                CampaignTarget.status == "active",
            )
        )).scalars().all()

    if not targets:
        campaign.status = "completed"
        await db.commit()
        return {"status": "completed", "message": "No active targets"}

    # Reset per-target daily counters at UTC midnight
    for t in targets:
        if not t.last_daily_reset or (now - t.last_daily_reset).days >= 1:
            t.enrolled_today = 0
            t.last_daily_reset = now

    # Round-robin team
    team = (await db.execute(
        select(User)
        .join(campaign_members, User.id == campaign_members.c.user_id)
        .where(campaign_members.c.campaign_id == campaign.id)
    )).scalars().all()
    if not team:
        raise HTTPException(status_code=400, detail="Campaign has no team members assigned")

    # Per-target allowance based on weight share. Cap at campaign-wide remaining.
    total_weight = sum(t.weight for t in targets) or 1
    remaining_total = campaign.max_prospects_per_day - campaign.prospects_today

    # Open a CampaignRun for this tick
    run = CampaignRun(campaign_id=campaign.id, started_at=now)
    db.add(run)
    await db.flush()

    summary_total = {
        "targets_processed": 0,
        "contacts_enrolled": 0,
        "skipped_dedup": 0,
        "skipped_filters": 0,
        "skipped_not_qualified": 0,
        "skipped_no_contact": 0,
    }
    per_target_summaries = []

    from app.runtime_config import get_google_maps_api_key
    maps_key = await get_google_maps_api_key(db)

    for target in targets:
        if remaining_total <= 0:
            break

        share = max(1, int((target.weight / total_weight) * campaign.max_prospects_per_day))
        target_allowance = max(0, share - target.enrolled_today)
        target_allowance = min(target_allowance, remaining_total)
        if target_allowance <= 0:
            continue

        target_counters = {
            "vertical": target.vertical, "location": target.location,
            "searched": 0, "enrolled": 0, "skipped_dedup": 0,
            "skipped_filters": 0, "skipped_not_qualified": 0, "skipped_no_contact": 0,
            "errors": 0,
        }

        # Search Maps for this target's pair
        if not maps_key:
            _log(db, campaign.id, "error", "Google Maps API key not configured")
            break

        try:
            businesses = await search_businesses(
                keyword=target.vertical, location=target.location,
                api_key=maps_key, max_results=40,
            )
        except Exception as e:
            _log(db, campaign.id, "error", f"Search failed for {target.vertical} in {target.location}: {str(e)[:80]}")
            target_counters["errors"] += 1
            per_target_summaries.append(target_counters)
            continue

        target_counters["searched"] = len(businesses)
        campaign.total_locations_searched += 1
        _log(db, campaign.id, "searched", f"Searching: {target.vertical} in {target.location} ({len(businesses)} results)")

        for biz in businesses:
            if target_counters["enrolled"] >= target_allowance:
                break
            try:
                outcome = await _process_business_through_pipeline(db, campaign, target.vertical, biz, team, now)
            except Exception as e:
                _log(db, campaign.id, "error", f"Pipeline error on {biz.name}: {str(e)[:80]}")
                outcome = "error"

            if outcome == "enrolled":
                target_counters["enrolled"] += 1
                campaign.prospects_today += 1
                target.enrolled_today += 1
                target.contacts_enrolled += 1
                remaining_total -= 1
                await db.commit()
            elif outcome == "skipped_dedup":
                target_counters["skipped_dedup"] += 1
            elif outcome == "skipped_filters":
                target_counters["skipped_filters"] += 1
            elif outcome == "skipped_not_qualified":
                target_counters["skipped_not_qualified"] += 1
            elif outcome == "skipped_no_contact":
                target_counters["skipped_no_contact"] += 1
            elif outcome == "error":
                target_counters["errors"] += 1

        # Update target meta
        target.last_run_at = now
        if target_counters["enrolled"] == 0:
            target.consecutive_empty_runs += 1
            if target.consecutive_empty_runs >= 3:
                target.status = "exhausted"
                target.paused_reason = "no_new_results_3_runs"
                _log(db, campaign.id, "exhausted",
                     f"Target exhausted: {target.vertical} in {target.location} (3 ticks with 0 enrolled)")
        else:
            target.consecutive_empty_runs = 0

        per_target_summaries.append(target_counters)
        summary_total["targets_processed"] += 1
        for k in ("skipped_dedup", "skipped_filters", "skipped_not_qualified", "skipped_no_contact"):
            summary_total[k] += target_counters[k]
        summary_total["contacts_enrolled"] += target_counters["enrolled"]

    # Finalize run row
    run.finished_at = datetime.now(timezone.utc)
    run.targets_processed = summary_total["targets_processed"]
    run.contacts_enrolled = summary_total["contacts_enrolled"]
    run.summary_json = json.dumps(per_target_summaries)

    # Campaign completion: all targets exhausted
    active_remaining = sum(1 for t in targets if t.status == "active")
    if active_remaining == 0:
        campaign.status = "completed"
        _log(db, campaign.id, "completed", "All targets exhausted — campaign complete")

    campaign.last_run_at = now
    await db.commit()

    return {
        "status": campaign.status,
        "prospects_today": campaign.prospects_today,
        "targets_processed": summary_total["targets_processed"],
        "contacts_enrolled": summary_total["contacts_enrolled"],
        "skipped": {k: v for k, v in summary_total.items() if k.startswith("skipped_")},
        "per_target": per_target_summaries,
    }


# ============================================================
# Internal cron endpoint — no auth, localhost only
# ============================================================

from fastapi import Request

@router.post("/{campaign_id}/run-batch-internal")
async def run_campaign_batch_internal(
    campaign_id: int,
    request: Request,
    db: AsyncSession = Depends(get_tenant_db),
):
    """
    Same as run-batch but without auth. Only accessible from localhost (cron).
    Cron uses the God Mode runner which iterates all active CampaignTargets
    per tick (concurrent multi-vertical / multi-geo). The UI manual-batch
    button still uses the legacy single-pair runner for now until God Mode
    is verified in prod.
    """
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="Internal endpoint — localhost only")

    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status != "running":
        return {"status": campaign.status, "message": f"Campaign is {campaign.status}"}

    return await _execute_god_mode_batch(campaign_id, db)


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
