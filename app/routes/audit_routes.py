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

    company_name = company.name if company else "Your Business"
    booking_url = settings.iclosed_booking_url

    # Show gated page: blurred preview + schedule to unlock
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Competitive Comparison — {_esc(company_name)}</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, sans-serif; background: #f5f7f5; color: #1a1a1a; }}
    .container {{ max-width: 700px; margin: 0 auto; padding: 20px; }}
    .header {{ background: linear-gradient(135deg, #0D3B13, #1B5E20); color: white; border-radius: 12px; padding: 32px; text-align: center; margin-bottom: 24px; }}
    .header img {{ width: 200px; margin-bottom: 12px; }}
    .blurred {{ filter: blur(8px); pointer-events: none; user-select: none; opacity: 0.6; padding: 20px; background: white; border-radius: 12px; margin-bottom: 24px; }}
    .blurred table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    .blurred td, .blurred th {{ padding: 8px; border-bottom: 1px solid #eee; }}
    .gate {{ background: white; border-radius: 12px; padding: 32px; text-align: center; box-shadow: 0 4px 24px rgba(0,0,0,0.1); }}
    .gate h2 {{ color: #1B5E20; margin-bottom: 8px; }}
    .gate p {{ color: #666; font-size: 14px; margin-bottom: 20px; }}
    .gate input {{ width: 100%; padding: 10px 14px; border: 1px solid #ddd; border-radius: 8px; font-size: 14px; margin-bottom: 12px; }}
    .gate button {{ width: 100%; padding: 14px; background: #E65100; color: white; border: none; border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer; }}
    .gate button:hover {{ background: #BF360C; }}
    .success {{ display: none; text-align: center; padding: 20px; }}
    .success h2 {{ color: #1B5E20; }}
</style>
</head><body>
<div class="container">
    <div class="header">
        <img src="https://backyardmarketingpros.com/wp-content/uploads/2024/09/BMP_Logo_Color_Horiz-White-1024x269.png" alt="BMP">
        <h1 style="font-size:22px">Your Competitive Comparison</h1>
        <p style="color:rgba(255,255,255,0.8)">{_esc(company_name)} vs. Top Competitors</p>
    </div>

    <!-- Blurred preview -->
    <div class="blurred">
        <table>
            <thead><tr><th></th><th>You</th><th>Competitor 1</th><th>Competitor 2</th><th>Competitor 3</th></tr></thead>
            <tbody>
                <tr><td>AI Findability</td><td>15</td><td>42</td><td>38</td><td>27</td></tr>
                <tr><td>Content Citability</td><td>22</td><td>55</td><td>48</td><td>31</td></tr>
                <tr><td>Local SEO</td><td>57</td><td>71</td><td>64</td><td>59</td></tr>
                <tr><td>Keywords Ranking</td><td>3</td><td>47</td><td>31</td><td>22</td></tr>
                <tr><td>Referring Domains</td><td>8</td><td>34</td><td>28</td><td>15</td></tr>
            </tbody>
        </table>
    </div>

    <!-- Gate form -->
    <div class="gate" id="gate-form">
        <h2>See How You Stack Up</h2>
        <p>Schedule a quick 15-minute call with our team to walk through your competitive analysis and discover exactly where the opportunities are.</p>
        <input type="text" id="gate-name" placeholder="Your name" required>
        <input type="email" id="gate-email" placeholder="Your email" required>
        <input type="tel" id="gate-phone" placeholder="Your phone number" required>
        <button onclick="submitGate()">Schedule & View Report</button>
        <p style="font-size:11px;color:#999;margin-top:8px">We'll send you a calendar invite for a brief walkthrough</p>
    </div>

    <!-- Success / redirect -->
    <div class="success" id="gate-success">
        <h2>You're booked!</h2>
        <p>Loading your competitive comparison...</p>
        <div style="margin:16px auto;width:40px;height:40px;border:4px solid #ddd;border-top-color:#1B5E20;border-radius:50%;animation:spin 1s linear infinite"></div>
        <style>@keyframes spin {{ to {{ transform: rotate(360deg); }} }}</style>
    </div>
</div>

<script>
async function submitGate() {{
    const name = document.getElementById('gate-name').value.trim();
    const email = document.getElementById('gate-email').value.trim();
    const phone = document.getElementById('gate-phone').value.trim();
    if (!name || !email || !phone) {{ alert('Please fill in all fields'); return; }}

    const btn = document.querySelector('.gate button');
    btn.textContent = 'Scheduling...';
    btn.disabled = true;

    try {{
        const res = await fetch('/api/report/{token}/book', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{ name, email, phone, token: '{token}' }}),
        }});
        const data = await res.json();
        if (data.success) {{
            document.getElementById('gate-form').style.display = 'none';
            document.getElementById('gate-success').style.display = 'block';
            // Redirect to competitors page after 3 seconds
            setTimeout(() => {{ window.location.href = '{competitors_url}'; }}, 3000);
        }} else {{
            // Still show the report even if booking API fails
            window.location.href = '{competitors_url}';
        }}
    }} catch(e) {{
        window.location.href = '{competitors_url}';
    }}
}}
</script>
</body></html>""")


# ============================================================
# Booking endpoint — called by the gate form
# ============================================================

from pydantic import BaseModel as _BM

class BookingRequest(_BM):
    name: str
    email: str
    phone: str
    token: str


@router.post("/api/report/{token}/book")
async def book_from_report(
    token: str,
    req: BookingRequest,
    db: AsyncSession = Depends(get_db),
):
    """Prospect submits the gate form — book via iClosed, update CRM contact, notify BDR."""
    report = (await db.execute(
        select(AuditReportModel).where(AuditReportModel.token == token)
    )).scalar_one_or_none()

    if not report:
        return {"success": False, "error": "Report not found"}

    company = (await db.execute(select(Company).where(Company.id == report.company_id))).scalar_one_or_none()

    # Parse name
    parts = req.name.strip().split(maxsplit=1)
    first_name = parts[0] if parts else req.name
    last_name = parts[1] if len(parts) > 1 else ""

    # Update contact in CRM with phone number
    from app.models import Contact
    if company:
        contacts = (await db.execute(
            select(Contact).where(Contact.company_id == company.id).order_by(Contact.is_primary.desc())
        )).scalars().all()

        if contacts:
            primary = contacts[0]
            if not primary.phone and req.phone:
                primary.phone = req.phone
            if not primary.email and req.email:
                primary.email = req.email
            if not primary.first_name and first_name:
                primary.first_name = first_name
            if not primary.last_name and last_name:
                primary.last_name = last_name
        else:
            import secrets as _s
            new_contact = Contact(
                company_id=company.id,
                first_name=first_name,
                last_name=last_name,
                email=req.email,
                phone=req.phone,
                is_primary=True,
                unsubscribe_token=_s.token_urlsafe(32),
            )
            db.add(new_contact)

    # Try to book via iClosed
    booking_result = None
    iclosed_key = settings.iclosed_api_key
    if iclosed_key:
        try:
            from app.services.iclosed import book_call
            booking_result = await book_call(
                api_key=iclosed_key,
                contact_email=req.email,
                contact_first_name=first_name,
                contact_last_name=last_name,
                contact_phone=req.phone,
                notes=f"Booked from competitive comparison report for {company.name if company else 'unknown'}",
            )
        except Exception:
            pass

    # Log + notify BDR
    if company:
        meeting_info = ""
        if booking_result and booking_result.success:
            meeting_info = f" — meeting booked"
            if booking_result.event_time:
                meeting_info = f" — meeting booked for {booking_result.event_time}"

        db.add(Activity(
            company_id=company.id,
            activity_type="meeting_booked",
            content=f"MEETING BOOKED: {req.name} ({req.phone}) scheduled from competitor report{meeting_info}",
        ))

        # Auto-advance deal
        from app.models import Deal
        from app.routes.deal_routes import STAGE_PROBABILITY, package_monthly_value
        deals = (await db.execute(
            select(Deal).where(
                Deal.company_id == company.id,
                Deal.stage.in_(("in_sequence", "prospecting", "qualified")),
            )
        )).scalars().all()
        for deal in deals:
            if deal.stage in ("in_sequence", "prospecting"):
                deal.stage = "qualified"
                deal.probability = STAGE_PROBABILITY.get("qualified", 25)
                if deal.value == 0 and deal.package:
                    deal.value = package_monthly_value(deal.package)

        # Create URGENT task for BDR
        if company.assigned_to:
            db.add(Task(
                company_id=company.id,
                user_id=company.assigned_to,
                description=f"MEETING BOOKED: {req.name} from {company.name} — phone: {req.phone} — scheduled from competitor report. Call to confirm!",
                due_date=datetime.now(timezone.utc),
            ))

        if company.status in ("new", "pursuing", "sequencing", "contacted"):
            company.status = "qualified"

    await db.commit()

    return {
        "success": True,
        "booked": bool(booking_result and booking_result.success),
        "message": "Meeting scheduled" if booking_result and booking_result.success else "Contact info captured",
    }


def _esc(s):
    if not s: return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


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
