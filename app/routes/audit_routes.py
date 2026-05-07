"""
AI Findability Audit Report routes.
Generates, stores, and serves branded HTML audit reports.
Public report page at /report/{token} — no auth needed (token IS the auth).
"""
from __future__ import annotations
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import User, Company, Activity, Task, AuditReportModel
from app.auth import get_current_user
from app.services.audit_report import generate_audit, render_report_html
from app.config import settings

router = APIRouter(tags=["audit"])


# ============================================================
# Generate report for a company (BDR action)
# ============================================================

@router.post("/api/companies/{company_id}/audit")
async def generate_audit_report(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate an AI Findability Audit report for a company."""
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if not company.website:
        raise HTTPException(status_code=400, detail="Company has no website to audit")

    # Check for existing report
    existing = (await db.execute(
        select(AuditReportModel).where(AuditReportModel.company_id == company_id)
    )).scalar_one_or_none()

    # Generate the audit
    report = await generate_audit(
        website=company.website,
        company_name=company.name,
        city=company.city or "",
        state=company.state or "",
        business_type=company.business_type or "",
        rating=company.rating or 0,
        review_count=company.review_count or 0,
    )

    token = existing.token if existing else secrets.token_urlsafe(16)
    public_url = settings.public_url.rstrip("/")
    html = render_report_html(report, token, public_url)

    if existing:
        existing.html_content = html
        existing.ai_findability_score = report.ai_findability_score
        existing.content_citability_score = report.content_citability_score
        existing.local_seo_score = report.local_seo_score
        existing.overall_grade = report.overall_grade
        existing.findings_json = __import__("json").dumps([{
            "type": f.get("type", ""),
            "severity": f.get("severity", "medium"),
            "detail": f.get("detail", ""),
            "angle": f.get("angle", ""),
        } for f in report.top_findings])
        existing.generated_at = datetime.now(timezone.utc)
    else:
        existing = AuditReportModel(
            company_id=company_id,
            token=token,
            html_content=html,
            ai_findability_score=report.ai_findability_score,
            content_citability_score=report.content_citability_score,
            local_seo_score=report.local_seo_score,
            overall_grade=report.overall_grade,
            findings_json=__import__("json").dumps([{
                "type": f.get("type", ""),
                "severity": f.get("severity", "medium"),
                "detail": f.get("detail", ""),
                "angle": f.get("angle", ""),
            } for f in report.top_findings]),
        )
        db.add(existing)

    db.add(Activity(
        company_id=company_id, user_id=user.id,
        activity_type="audit_generated",
        content=f"AI Findability Audit generated — Score: {report.ai_findability_score}/100, Grade: {report.overall_grade}",
    ))

    await db.commit()
    await db.refresh(existing)

    report_url = f"{public_url}/report/{token}"

    return {
        "report_id": existing.id,
        "token": token,
        "url": report_url,
        "ai_findability_score": report.ai_findability_score,
        "content_citability_score": report.content_citability_score,
        "local_seo_score": report.local_seo_score,
        "overall_grade": report.overall_grade,
        "top_findings": report.top_findings[:5],
    }


# ============================================================
# Public report page — no auth (token is the auth)
# ============================================================

@router.get("/report/{token}", response_class=HTMLResponse)
async def view_report(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Serve the audit report HTML. Public — no auth needed."""
    report = (await db.execute(
        select(AuditReportModel).where(AuditReportModel.token == token)
    )).scalar_one_or_none()

    if not report:
        return HTMLResponse("<h1>Report not found</h1>", status_code=404)

    # Track view count
    report.view_count = (report.view_count or 0) + 1
    report.last_viewed_at = datetime.now(timezone.utc)
    await db.commit()

    return HTMLResponse(report.html_content)


# ============================================================
# Report view tracking beacon (called by JS in the report)
# ============================================================

@router.post("/api/track/report-view")
async def track_report_view(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Track when a prospect views their audit report. Creates a hot-lead task."""
    try:
        body = await request.json()
        token = body.get("token", "")
    except Exception:
        return {"status": "ok"}

    if not token:
        return {"status": "ok"}

    report = (await db.execute(
        select(AuditReportModel).where(AuditReportModel.token == token)
    )).scalar_one_or_none()

    if not report:
        return {"status": "ok"}

    # Log the view as an activity
    company = (await db.execute(select(Company).where(Company.id == report.company_id))).scalar_one_or_none()
    if company:
        db.add(Activity(
            company_id=company.id,
            activity_type="report_viewed",
            content=f"Prospect viewed AI Findability Audit (view #{report.view_count or 1})",
        ))

        # Auto-create a hot-lead task if this is the first view
        if (report.view_count or 0) <= 1 and company.assigned_to:
            db.add(Task(
                company_id=company.id,
                user_id=company.assigned_to,
                description=f"HOT: {company.name} just opened their AI Findability Audit — follow up now",
                due_date=datetime.now(timezone.utc),
            ))

        await db.commit()

    return {"status": "ok"}


# ============================================================
# Competitor comparison trigger
# ============================================================

@router.get("/report/{token}/competitors", response_class=HTMLResponse)
async def view_competitor_report(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Serve the stored competitor comparison report if it exists."""
    report = (await db.execute(
        select(AuditReportModel).where(AuditReportModel.token == token)
    )).scalar_one_or_none()

    if not report or not report.competitor_html:
        return HTMLResponse("<html><body><div style='font-family:sans-serif;text-align:center;padding:60px'><h2>Report is being generated...</h2><p>Check back in a few minutes.</p></div></body></html>")

    return HTMLResponse(report.competitor_html)


@router.get("/report/{token}/compare", response_class=HTMLResponse)
async def request_competitor_comparison(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """
    When prospect clicks "See Your Competitive Comparison":
    1. If competitor report already exists → serve it
    2. If not → show "We're on it!", fire background generation, create BDR task
    """
    import asyncio

    report = (await db.execute(
        select(AuditReportModel).where(AuditReportModel.token == token)
    )).scalar_one_or_none()

    if not report:
        return HTMLResponse("<h1>Report not found</h1>", status_code=404)

    # If competitor report already generated, serve it directly
    if report.competitor_html:
        return HTMLResponse(report.competitor_html)

    company = (await db.execute(select(Company).where(Company.id == report.company_id))).scalar_one_or_none()

    if company:
        db.add(Activity(
            company_id=company.id,
            activity_type="competitor_comparison_requested",
            content=f"Prospect requested competitive comparison from audit report",
        ))

        if company.assigned_to:
            db.add(Task(
                company_id=company.id,
                user_id=company.assigned_to,
                description=f"URGENT: {company.name} wants competitive comparison — high-intent signal, call them",
                due_date=datetime.now(timezone.utc),
            ))

        if company.status in ("new", "pursuing", "sequencing"):
            company.status = "qualified"

        await db.commit()

    # Fire background generation
    asyncio.create_task(_generate_competitor_report_bg(report.id))

    public_url = settings.public_url.rstrip("/")
    competitors_url = f"{public_url}/report/{token}/competitors"

    # Show a branded "generating" page that auto-refreshes to the competitors page
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="30;url={competitors_url}">
<title>Competitive Comparison — {company.name if company else 'Report'}</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, sans-serif; background: #f5f7f5; display: flex; justify-content: center; align-items: center; min-height: 100vh; width: 100%; }}
    .card {{ background: white; border-radius: 16px; padding: 48px; max-width: 500px; width: 90%; margin: 0 auto; text-align: center; box-shadow: 0 4px 24px rgba(0,0,0,0.1); }}
    .card img {{ width: 200px; margin-bottom: 24px; }}
    .card h1 {{ color: #1B5E20; font-size: 24px; margin-bottom: 12px; }}
    .card p {{ color: #666; font-size: 14px; line-height: 1.6; }}
    .spinner {{ display: inline-block; width: 40px; height: 40px; border: 4px solid #ddd; border-top-color: #1B5E20; border-radius: 50%; animation: spin 1s linear infinite; margin-bottom: 16px; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style>
</head><body>
<div class="card">
    <img src="https://backyardmarketingpros.com/wp-content/uploads/2024/08/BMP_Logo_Color_Horiz-1024x269.png" alt="BMP">
    <div class="spinner"></div>
    <h1>Building your competitive comparison...</h1>
    <p>We're auditing the top businesses in your market right now. This usually takes about 30 seconds — this page will automatically update when it's ready.</p>
    <p style="margin-top:16px;color:#888;font-size:12px">Usually takes less than 24 hours</p>
</div>
</body></html>""")


# ============================================================
# Get report data for a company (internal, for BDR dashboard)
# ============================================================

@router.get("/api/companies/{company_id}/audit")
async def get_company_audit(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get existing audit report data for a company (if one has been generated)."""
    report = (await db.execute(
        select(AuditReportModel).where(AuditReportModel.company_id == company_id)
    )).scalar_one_or_none()

    if not report:
        return {"exists": False}

    import json
    public_url = settings.public_url.rstrip("/")

    return {
        "exists": True,
        "report_id": report.id,
        "token": report.token,
        "url": f"{public_url}/report/{report.token}",
        "ai_findability_score": report.ai_findability_score,
        "content_citability_score": report.content_citability_score,
        "local_seo_score": report.local_seo_score,
        "overall_grade": report.overall_grade,
        "top_findings": json.loads(report.findings_json) if report.findings_json else [],
        "view_count": report.view_count or 0,
        "last_viewed_at": report.last_viewed_at.isoformat() if report.last_viewed_at else None,
        "generated_at": report.generated_at.isoformat() if report.generated_at else None,
        "has_competitor_report": bool(report.competitor_html),
        "competitor_url": f"{settings.public_url.rstrip('/')}/report/{report.token}/competitors" if report.competitor_html else None,
    }


# ============================================================
# Background competitor report generation
# ============================================================

async def _generate_competitor_report_bg(report_id: int):
    """Background task: audit top 3 SERP competitors and store the comparison report."""
    import logging
    log = logging.getLogger("bmp")

    try:
        from app.database import async_session
        from app.services.competitor_report import audit_competitor, render_comparison_html
        from app.services.dataforseo import serp_check

        async with async_session() as db:
            report = (await db.execute(
                select(AuditReportModel).where(AuditReportModel.id == report_id)
            )).scalar_one_or_none()
            if not report:
                return

            company = (await db.execute(
                select(Company).where(Company.id == report.company_id)
            )).scalar_one_or_none()
            if not company:
                return

            dfs_login = settings.dataforseo_login
            dfs_pass = settings.dataforseo_password
            if not dfs_login or not dfs_pass:
                log.warning("No DataForSEO credentials — can't generate competitor report")
                return

            prospect = {
                "name": company.name,
                "website": company.website or "",
                "ai_findability_score": report.ai_findability_score,
                "content_citability_score": report.content_citability_score,
                "local_seo_score": report.local_seo_score,
                "ranked_keywords": 0,
                "referring_domains": 0,
                "domain_rank": 0,
                "has_llms_txt": False,
                "has_faq_schema": False,
                "has_local_business_schema": False,
            }

            # Search for competitors
            competitors = []
            if company.business_type and company.city:
                search_term = f"{company.business_type} {company.city} {company.state or ''}".strip()
                location = f"{company.city},{company.state},United States" if company.state else f"{company.city},United States"

                serp = await serp_check(search_term, location, dfs_login, dfs_pass)
                if serp and serp.competitors:
                    prospect_domain = (company.website or "").replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")
                    others = [c for c in serp.competitors if prospect_domain not in c.domain][:3]

                    for comp in others:
                        url = comp.url if comp.url.startswith("http") else f"https://{comp.domain}"
                        comp_data = await audit_competitor(url, comp.title or comp.domain, dfs_login, dfs_pass)
                        competitors.append(comp_data)

            if competitors:
                html = render_comparison_html(
                    prospect=prospect,
                    competitors=competitors,
                    company_name=company.name,
                    city=company.city or "",
                    state=company.state or "",
                    business_type=company.business_type or "",
                )
                report.competitor_html = html
                report.competitor_generated_at = datetime.now(timezone.utc)

                # Create task notifying BDR that the report is ready
                if company.assigned_to:
                    public_url = settings.public_url.rstrip("/")
                    db.add(Task(
                        company_id=company.id,
                        user_id=company.assigned_to,
                        description=f"Competitor report ready for {company.name} — send to prospect: {public_url}/report/{report.token}/competitors",
                        due_date=datetime.now(timezone.utc),
                    ))

                db.add(Activity(
                    company_id=company.id,
                    activity_type="competitor_report_generated",
                    content=f"Competitor comparison report auto-generated ({len(competitors)} competitors audited)",
                ))

                await db.commit()
                log.info(f"Competitor report generated for {company.name} ({len(competitors)} competitors)")
            else:
                log.warning(f"No competitors found for {company.name} in SERP")

    except Exception as e:
        import logging
        logging.getLogger("bmp").exception(f"Background competitor report failed: {e}")
