from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from app.database import get_db
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
    db: AsyncSession = Depends(get_db),
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


@router.get("/history")
async def get_search_history(
    db: AsyncSession = Depends(get_db),
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
