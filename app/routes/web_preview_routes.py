"""
Web preview routes.

Two distinct surfaces:
  - /api/web-previews/*       authenticated, tenant-scoped, rep-facing
  - /sitepreview/{url_slug}   public, no auth, the actual served preview

The public route handles the inbound prospect click. Tenant resolution
on the public route comes from the WebPreview row itself (it carries
tenant_id), NOT from the Host header — a single
sitepreview.leadprospector.ai catches every tenant's previews today.
White-label sitepreview.{tenant-domain} routing lands in a later commit.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import Company, User, WebPreview
from app.tenancy import get_tenant_db, get_current_tenant_id

log = logging.getLogger("bmp.web_preview_routes")

router = APIRouter(prefix="/api/web-previews", tags=["web-previews"])
public_router = APIRouter(tags=["web-previews-public"])


# ----------------------------------------------------------------------
# Authenticated routes (rep-facing)
# ----------------------------------------------------------------------

class GeneratePreviewRequest(BaseModel):
    company_id: int
    template_override: Optional[str] = None
    cta_url_override: Optional[str] = None


class PreviewOut(BaseModel):
    id: int
    company_id: int
    template_slug: str
    url_slug: str
    public_url: Optional[str] = None  # computed by caller, not a DB column
    view_count: int
    cta_click_count: int
    first_viewed_at: Optional[datetime] = None
    last_viewed_at: Optional[datetime] = None
    created_at: datetime
    status: str

    class Config:
        from_attributes = True


def _public_url(request: Request, url_slug: str) -> str:
    """Build the public-facing URL the rep pastes into an email.

    Today everything goes through sitepreview.leadprospector.ai. Tenant
    white-label (e.g. sitepreview.backyardmarketingpros.com) lands in a
    later commit — same path, different host derived from
    tenant_domains.
    """
    return f"https://sitepreview.leadprospector.ai/{url_slug}"


@router.post("/generate", response_model=PreviewOut)
async def generate_preview(
    req: GeneratePreviewRequest,
    request: Request,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    tenant_id: int = Depends(get_current_tenant_id),
):
    """One-click generation. Reads the company, calls the LLM via
    web_preview_generator, persists the rendered HTML + slot data,
    returns the public URL."""
    if user.role not in ("admin", "super_admin", "senior_rep", "sales_rep"):
        raise HTTPException(status_code=403, detail="Not allowed")

    company = (await db.execute(
        select(Company).where(Company.id == req.company_id)
    )).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    # Per-tenant brand for the footer + CTA. RuntimeConfig is auto-filter
    # scoped to the tenant; we read the current tenant's brand here.
    from app.runtime_config import get_org_brand
    brand = await get_org_brand(db)
    agency_name = brand.get("company_name") or "Your agency"
    agency_url = brand.get("website_url") or ""

    # CTA URL: req override → per-tenant default → fallback to website
    # for now. (Later we add a dedicated per-tenant preview_cta_url.)
    cta_url = (req.cta_url_override or agency_url or "").strip()

    # Photos: for v1 we pass empty lists; data assembly inside
    # web_preview_generator falls back to a known-good Unsplash url so
    # the preview always renders. Wiring Places + Unsplash live happens
    # in the next iteration.
    from app.services.web_preview_generator import generate_web_preview
    result = await generate_web_preview(
        company=company,
        agency_name=agency_name,
        agency_url=agency_url,
        cta_url=cta_url,
        places_photos=[],
        unsplash_fallback=[],
        template_override=req.template_override,
    )
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])

    expires = datetime.now(timezone.utc) + timedelta(days=30)
    preview = WebPreview(
        tenant_id=tenant_id,
        company_id=company.id,
        created_by=user.id,
        template_slug=result["template_slug"],
        url_slug=result["slug"],
        html=result["html"],
        slots_json=json.dumps(result["slots"]),
        photos_json=json.dumps(result["photos"]),
        cta_url=cta_url,
        cost_usd=float(result.get("cost_estimate_usd") or 0),
        status="active",
        expires_at=expires,
    )
    db.add(preview)
    await db.commit()
    await db.refresh(preview)

    out = PreviewOut.model_validate(preview, from_attributes=True).model_dump()
    out["public_url"] = _public_url(request, preview.url_slug)
    return out


@router.get("/by-company/{company_id}", response_model=list[PreviewOut])
async def list_company_previews(
    company_id: int,
    request: Request,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """All previews for one company (so the rep can see history + reuse)."""
    rows = (await db.execute(
        select(WebPreview)
        .where(WebPreview.company_id == company_id)
        .order_by(WebPreview.created_at.desc())
    )).scalars().all()
    out = []
    for p in rows:
        d = PreviewOut.model_validate(p, from_attributes=True).model_dump()
        d["public_url"] = _public_url(request, p.url_slug)
        out.append(d)
    return out


# ----------------------------------------------------------------------
# Public route (the prospect's click)
# ----------------------------------------------------------------------

@public_router.get("/sitepreview/{url_slug}", response_class=HTMLResponse)
async def serve_preview(
    url_slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # no tenant scope — public
):
    """Inbound prospect click. Looks up the preview by slug, increments
    view counters, returns the stored HTML.

    Cross-tenant by design — anyone can hit this URL. The slug itself
    is the auth token (8+ chars of unique slugified-name + random
    token; not guessable in any sensible cold-outreach attack model).
    """
    p = (await db.execute(
        select(WebPreview).where(WebPreview.url_slug == url_slug)
    )).scalar_one_or_none()

    if not p or p.status != "active":
        return HTMLResponse(
            _not_found_html(), status_code=404,
            headers={"Cache-Control": "no-store"},
        )
    if p.expires_at and p.expires_at < datetime.now(timezone.utc):
        return HTMLResponse(
            _expired_html(), status_code=410,
            headers={"Cache-Control": "no-store"},
        )

    now = datetime.now(timezone.utc)
    p.view_count += 1
    if p.first_viewed_at is None:
        p.first_viewed_at = now
    p.last_viewed_at = now
    await db.commit()

    return HTMLResponse(
        p.html,
        headers={
            "Cache-Control": "private, max-age=300",
            # Lock down the preview from being framed elsewhere.
            "X-Frame-Options": "SAMEORIGIN",
        },
    )


def _not_found_html() -> str:
    return """<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Not found</title>
<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#0a0e14;color:#e6edf3;text-align:center;padding:24px}
.card{max-width:480px}h1{font-size:22px;margin-bottom:8px}p{color:#8b949e;font-size:14px}</style>
</head><body><div class="card"><h1>This preview isn't available.</h1>
<p>The link may have expired or been removed by the agency that sent it.</p></div></body></html>"""


def _expired_html() -> str:
    return """<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Expired</title>
<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#0a0e14;color:#e6edf3;text-align:center;padding:24px}
.card{max-width:480px}h1{font-size:22px;margin-bottom:8px}p{color:#8b949e;font-size:14px}</style>
</head><body><div class="card"><h1>This preview has expired.</h1>
<p>Reach out to the agency that sent you this link for a fresh version.</p></div></body></html>"""
