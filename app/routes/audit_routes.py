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

@router.get("/report/{token}/compare", response_class=HTMLResponse)
async def request_competitor_comparison(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """
    When prospect clicks "See Your Competitive Comparison" in the report.
    Creates a CRM task for the BDR team and shows a confirmation page.
    """
    report = (await db.execute(
        select(AuditReportModel).where(AuditReportModel.token == token)
    )).scalar_one_or_none()

    if not report:
        return HTMLResponse("<h1>Report not found</h1>", status_code=404)

    company = (await db.execute(select(Company).where(Company.id == report.company_id))).scalar_one_or_none()

    if company:
        # Log the engagement
        db.add(Activity(
            company_id=company.id,
            activity_type="competitor_comparison_requested",
            content=f"Prospect requested competitive comparison from audit report",
        ))

        # Create urgent task for BDR
        if company.assigned_to:
            db.add(Task(
                company_id=company.id,
                user_id=company.assigned_to,
                description=f"URGENT: {company.name} wants to see competitive comparison — high-intent signal, call them",
                due_date=datetime.now(timezone.utc),
            ))

        # Qualify the lead if not already
        if company.status in ("new", "pursuing", "sequencing"):
            company.status = "qualified"

        await db.commit()

    # Show a branded confirmation page
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Competitive Comparison — {company.name if company else 'Report'}</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, sans-serif; background: #f5f7f5; display: flex; justify-content: center; align-items: center; min-height: 100vh; width: 100%; }}
    .card {{ background: white; border-radius: 16px; padding: 48px; max-width: 500px; width: 90%; margin: 0 auto; text-align: center; box-shadow: 0 4px 24px rgba(0,0,0,0.1); }}
    .card img {{ width: 200px; margin-bottom: 24px; }}
    .card h1 {{ color: #1B5E20; font-size: 24px; margin-bottom: 12px; }}
    .card p {{ color: #666; font-size: 14px; line-height: 1.6; }}
    .check {{ font-size: 48px; margin-bottom: 16px; }}
</style>
</head><body>
<div class="card">
    <img src="https://backyardmarketingpros.com/wp-content/uploads/2024/08/BMP_Logo_Color_Horiz-1024x269.png" alt="BMP">
    <div class="check">&#x2705;</div>
    <h1>We're on it!</h1>
    <p>We're putting together your competitive comparison right now. One of our team members will send it over to you shortly — along with some insights on where you stand vs. your top competitors.</p>
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
    }
