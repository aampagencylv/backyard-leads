from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from app.tenancy import get_tenant_db
from app.models import User, Search, Company
from app.auth import get_current_user
from app.config import settings
from app.services.map_scraper import search_businesses
from app.services.website_intel import analyze_website, analysis_to_dict
import json

router = APIRouter(prefix="/api/search", tags=["search"])


class SearchRequest(BaseModel):
    keyword: str  # e.g. "pool builders"
    location: str  # e.g. "Austin, TX"
    max_results: int = 20


class SearchResponse(BaseModel):
    search_id: int
    keyword: str
    location: str
    results_count: int
    message: str


@router.post("/", response_model=SearchResponse)
async def create_search(
    req: SearchRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Start a new lead search. Scrapes maps and returns businesses."""
    from app.runtime_config import get_google_maps_api_key
    maps_key = await get_google_maps_api_key(db)
    if not maps_key:
        raise HTTPException(status_code=500, detail="Google Maps API key not configured")

    # Create search record
    search = Search(
        user_id=user.id,
        keyword=req.keyword,
        location=req.location,
    )
    db.add(search)
    await db.commit()
    await db.refresh(search)

    # Run the map scraper
    businesses = await search_businesses(
        keyword=req.keyword,
        location=req.location,
        api_key=maps_key,
        max_results=req.max_results,
    )

    # Save companies to database
    def _clean_website(url):
        """Strip UTM params and tracking query strings from website URLs."""
        if not url:
            return url
        from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
        p = urlparse(url)
        # Remove utm_*, gclid, fbclid, etc — keep any meaningful query params
        clean_params = {k: v for k, v in parse_qs(p.query).items()
                        if not k.startswith(('utm_', 'gclid', 'fbclid', 'mc_', 'ref'))}
        clean_query = urlencode(clean_params, doseq=True) if clean_params else ""
        return urlunparse((p.scheme, p.netloc, p.path.rstrip('/'), p.params, clean_query, ""))

    for biz in businesses:
        company = Company(
            search_id=search.id,
            name=biz.name,
            phone=biz.phone,
            website=_clean_website(biz.website),
            address=biz.address,
            city=biz.city,
            state=biz.state,
            rating=biz.rating,
            review_count=biz.review_count,
            business_type=biz.business_type,
        )
        db.add(company)

    search.results_count = len(businesses)
    await db.commit()

    return SearchResponse(
        search_id=search.id,
        keyword=req.keyword,
        location=req.location,
        results_count=len(businesses),
        message=f"Found {len(businesses)} businesses. Use /api/companies to view and enrich them.",
    )


class YellowPagesSearchRequest(BaseModel):
    keyword: str
    location: str  # "City, ST" or ZIP
    pages: int = 1  # ~30 results per page


@router.post("/yellow-pages")
async def yellow_pages_search_endpoint(
    req: YellowPagesSearchRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Alternative SMB lead source via Netrows /businesses/search.
    Useful for rural-market or trade-specific searches that come up
    thin on Google Maps. Same insert flow as the Maps search — every
    result lands in companies with status='new' so the BDR can scan
    + Pursue from the Companies page."""
    from app.runtime_config import get_netrows_api_key
    nr_key = await get_netrows_api_key(db)
    if not nr_key:
        raise HTTPException(status_code=400, detail="Netrows API key not configured")

    from app.services.netrows_enrichment import yellow_pages_search
    from app.services.domain_utils import normalize_domain

    # Pull `pages` pages worth of results (cap at 3 to avoid runaway)
    pages = max(1, min(3, int(req.pages or 1)))
    all_results = []
    for page in range(1, pages + 1):
        results = await yellow_pages_search(req.keyword, req.location, nr_key, page=page)
        if not results:
            break
        all_results.extend(results)

    # Persist the search history row alongside the Maps searches so
    # they share the activity feed.
    search = Search(user_id=user.id, keyword=f"YP: {req.keyword}", location=req.location)
    db.add(search)
    await db.flush()

    inserted = 0
    skipped_dupes = 0
    for biz in all_results:
        if not biz.name:
            continue
        # Domain-dedupe against existing companies
        if biz.website:
            dom = normalize_domain(biz.website)
            if dom:
                existing = (await db.execute(
                    select(Company).where(Company.domain == dom).limit(1)
                )).scalar_one_or_none()
                if existing:
                    skipped_dupes += 1
                    continue
        db.add(Company(
            search_id=search.id,
            name=biz.name,
            phone=biz.phone,
            website=biz.website,
            address=biz.street,
            city=biz.city,
            state=biz.state,
            domain=normalize_domain(biz.website) if biz.website else None,
            rating=biz.rating,
            review_count=biz.review_count,
            business_type=", ".join(biz.categories[:3]) if biz.categories else req.keyword,
            status="new",
        ))
        inserted += 1
    search.results_count = inserted
    await db.commit()

    return {
        "search_id": search.id,
        "source": "yellow_pages",
        "keyword": req.keyword,
        "location": req.location,
        "raw_results": len(all_results),
        "inserted": inserted,
        "skipped_dupes": skipped_dupes,
    }


@router.get("/history")
async def get_search_history(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Get all past searches for this user."""
    result = await db.execute(
        select(Search)
        .where(Search.user_id == user.id)
        .order_by(Search.created_at.desc())
    )
    searches = result.scalars().all()
    return [
        {
            "id": s.id,
            "keyword": s.keyword,
            "location": s.location,
            "results_count": s.results_count,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in searches
    ]
