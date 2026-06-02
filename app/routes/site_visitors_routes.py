"""
Website visitor identification — manager-facing endpoints.

GET  /api/site-visitors/recent   — feed of identified company visits
GET  /api/site-visitors/sessions/{id}/pageviews
POST /api/site-visitors/sessions/{id}/convert-to-company

Admin / super_admin only — the visitor IDs are tied to org-wide data
and not scoped per rep.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.tenancy import get_tenant_db
from app.auth import get_current_user
from app.models import User, Company, Contact, SiteVisitorSession, PageView, Activity

router = APIRouter(prefix="/api/site-visitors", tags=["site-visitors"])
log = logging.getLogger("bmp.site_visitors")


def _check_admin(user: User) -> None:
    if user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")


@router.get("/recent")
async def list_recent_visitors(
    days: int = Query(14, ge=1, le=90),
    include_isp: bool = Query(False, description="Include ISP/residential IPs (noisy)"),
    include_unresolved: bool = Query(False, description="Include sessions we couldn't resolve"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """List recent identified site visitors, grouped by resolved company
    when possible. Returns one row per session — we don't merge
    sessions across bvids since the cookie IS the visitor.

    Filters:
      - `include_isp=false` (default) hides sessions whose IP looks
        like a residential ISP (Comcast, Verizon, etc.). These are
        noise unless the user explicitly wants to scan.
      - `include_unresolved=false` (default) hides sessions where we
        didn't get an org/domain at all. Most of these are bots.
    """
    _check_admin(user)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    q = select(SiteVisitorSession).where(SiteVisitorSession.last_seen_at >= cutoff)
    if not include_isp:
        q = q.where(SiteVisitorSession.is_isp_ip == False)
    if not include_unresolved:
        q = q.where(
            (SiteVisitorSession.resolved_company_id.isnot(None))
            | (SiteVisitorSession.resolved_domain.isnot(None))
            | (SiteVisitorSession.resolved_company_name.isnot(None))
        )
    q = q.order_by(SiteVisitorSession.last_seen_at.desc()).limit(limit)
    sessions = (await db.execute(q)).scalars().all()

    # Prefetch companies in one shot
    company_ids = {s.resolved_company_id for s in sessions if s.resolved_company_id}
    company_map = {}
    contacts_count_map: dict[int, int] = {}
    if company_ids:
        co_rows = (await db.execute(
            select(Company).where(Company.id.in_(company_ids))
        )).scalars().all()
        company_map = {c.id: c for c in co_rows}
        # Contact counts per matched company — tells the BDR if we
        # already have someone to call/email.
        rows = (await db.execute(
            select(Contact.company_id, func.count(Contact.id))
            .where(Contact.company_id.in_(company_ids))
            .group_by(Contact.company_id)
        )).all()
        contacts_count_map = {cid: cnt for cid, cnt in rows}

    out = []
    for s in sessions:
        matched = company_map.get(s.resolved_company_id) if s.resolved_company_id else None
        out.append({
            "session_id": s.id,
            "bvid": s.bvid,
            "first_seen_at": s.first_seen_at.isoformat() if s.first_seen_at else None,
            "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None,
            "pageview_count": s.pageview_count or 0,
            "ip": s.ip,
            "country": s.country,
            "region": s.region,
            "city": s.city,
            "is_isp_ip": bool(s.is_isp_ip),
            "resolved_company_name": s.resolved_company_name,
            "resolved_domain": s.resolved_domain,
            "matched_company": {
                "id": matched.id,
                "name": matched.name,
                "website": matched.website,
                "city": matched.city,
                "state": matched.state,
                "contacts_count": contacts_count_map.get(matched.id, 0),
            } if matched else None,
        })

    # Headline KPIs at the top of the visitor list
    total_sessions = (await db.execute(
        select(func.count(SiteVisitorSession.id)).where(SiteVisitorSession.last_seen_at >= cutoff)
    )).scalar_one() or 0
    resolved_sessions = (await db.execute(
        select(func.count(SiteVisitorSession.id)).where(
            SiteVisitorSession.last_seen_at >= cutoff,
            SiteVisitorSession.is_isp_ip == False,
            SiteVisitorSession.resolved_domain.isnot(None),
        )
    )).scalar_one() or 0
    matched_companies = (await db.execute(
        select(func.count(func.distinct(SiteVisitorSession.resolved_company_id))).where(
            SiteVisitorSession.last_seen_at >= cutoff,
            SiteVisitorSession.resolved_company_id.isnot(None),
        )
    )).scalar_one() or 0
    return {
        "window_days": days,
        "totals": {
            "total_sessions": total_sessions,
            "resolved_sessions": resolved_sessions,
            "matched_companies": matched_companies,
        },
        "sessions": out,
    }


@router.get("/sessions/{session_id}/pageviews")
async def session_pageviews(
    session_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Drilldown — list the pages this visitor session viewed."""
    _check_admin(user)
    session = (await db.execute(
        select(SiteVisitorSession).where(SiteVisitorSession.id == session_id)
    )).scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    pvs = (await db.execute(
        select(PageView).where(PageView.visitor_token == session.bvid)
        .order_by(PageView.created_at.desc())
        .limit(200)
    )).scalars().all()
    return {
        "session_id": session.id,
        "bvid": session.bvid,
        "pageviews": [
            {
                "id": p.id,
                "url": p.url,
                "title": p.page_title,
                "referrer": p.referrer,
                "event_type": p.event_type,
                "event_label": p.event_label,
                "event_value": p.event_value,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in pvs
        ],
    }


class ConvertRequest(BaseModel):
    # Optional manual overrides if the resolver picked something weird
    name: Optional[str] = None
    website: Optional[str] = None


@router.post("/sessions/{session_id}/convert-to-company")
async def convert_session_to_company(
    session_id: int,
    req: ConvertRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Promote a resolved visitor session into a real Company record so
    the rep can enrich + sequence it like any other lead. If a Company
    with the same domain already exists, we just attach the session to
    it (no duplicate)."""
    _check_admin(user)
    session = (await db.execute(
        select(SiteVisitorSession).where(SiteVisitorSession.id == session_id)
    )).scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    name = (req.name or session.resolved_company_name or "").strip()
    domain = (req.website or session.resolved_domain or "").strip().lower()
    if not name and not domain:
        raise HTTPException(status_code=400, detail="No company name or domain to convert")
    if not name:
        name = domain  # fall back to domain as name placeholder

    # Reuse existing Company when domain matches
    existing = None
    if domain:
        existing = (await db.execute(
            select(Company).where(Company.domain == domain).limit(1)
        )).scalar_one_or_none()

    if existing:
        company = existing
    else:
        website = domain if not domain.startswith("http") else domain
        if website and not website.startswith("http"):
            website = f"https://{website}"
        company = Company(
            name=name[:500],
            domain=domain or None,
            website=website or None,
            city=session.city,
            state=session.region,
            status="new",
            assigned_to=user.id,
        )
        db.add(company)
        await db.flush()
        db.add(Activity(
            company_id=company.id,
            user_id=user.id,
            activity_type="company_created",
            content=f"Created from site visitor session #{session.id} ({session.pageview_count} pageviews) — {session.resolved_company_name or session.resolved_domain}",
        ))

    session.resolved_company_id = company.id
    if not session.resolved_company_name:
        session.resolved_company_name = company.name
    await db.commit()

    return {
        "ok": True,
        "company_id": company.id,
        "company_name": company.name,
        "session_id": session.id,
    }
