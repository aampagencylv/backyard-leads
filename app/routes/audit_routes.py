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


async def _resolve_audit_booking_url(db, rc, public_url: str) -> str:
    """Pick the right Schedule-a-Call destination based on the org's
    audit_scheduler_type setting. Falls back to '' (caller will then
    use the default iClosed URL)."""
    scheduler_type = (getattr(rc, "audit_scheduler_type", None) or "iclosed").lower()
    if scheduler_type == "iclosed":
        return ""  # render_report_html falls back to settings.iclosed_booking_url
    if scheduler_type == "custom":
        return (getattr(rc, "audit_custom_url", "") or "").strip()
    if scheduler_type == "native":
        user_id = getattr(rc, "audit_native_user_id", None)
        if not user_id:
            return ""  # not configured yet → fall back to iClosed
        host = (await db.execute(
            select(User).where(User.id == int(user_id), User.is_active == True)
        )).scalar_one_or_none()
        if host and host.booking_slug and host.google_refresh_token:
            return f"{public_url.rstrip('/')}/book/{host.booking_slug}"
        # Picked user no longer has a booking page → fall back
        return ""
    return ""


def _const_eq(a: str, b: str) -> bool:
    """Constant-time string compare. Use for webhook-secret checks so
    timing differences don't leak the secret one byte at a time."""
    import hmac
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


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
    # Pull org-level audit-report branding overrides (header banner,
    # footer logo, side panels, scheduler choice). Empty values fall
    # back to BMP defaults.
    from app.runtime_config import _get_or_create as _get_rc
    rc = await _get_rc(db)
    booking_url = await _resolve_audit_booking_url(db, rc, public_url)
    html = render_report_html(
        report, token, public_url,
        header_url=getattr(rc, "audit_report_header_url", "") or "",
        footer_logo_url=getattr(rc, "audit_report_logo_url", "") or "",
        left_image_url=getattr(rc, "audit_left_image_url", "") or "",
        left_message=getattr(rc, "audit_left_message", "") or "",
        right_image_url=getattr(rc, "audit_right_image_url", "") or "",
        right_message=getattr(rc, "audit_right_message", "") or "",
        booking_url_override=booking_url,
    )

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
    """Serve the competitor comparison report.

    State machine:
      - HTML exists                        → serve it
      - Booked but generation in flight    → branded polling page
      - Not booked or token unknown        → "please book first"
    """
    report = (await db.execute(
        select(AuditReportModel).where(AuditReportModel.token == token)
    )).scalar_one_or_none()
    if not report:
        return HTMLResponse(
            "<html><body><div style='font-family:sans-serif;text-align:center;padding:60px'>"
            "<h2>Report not found</h2></div></body></html>",
            status_code=404,
        )

    # Done — serve it
    if report.competitor_html:
        return HTMLResponse(report.competitor_html)

    # Booked but generation still running — serve the polling page
    if report.booked_at:
        company = (await db.execute(
            select(Company).where(Company.id == report.company_id)
        )).scalar_one_or_none()
        company_name = company.name if company else "Your Business"
        return HTMLResponse(_render_generating_page(token, company_name))

    # Not booked — bounce them back to the gate
    public_url = settings.public_url.rstrip("/")
    compare_url = f"{public_url}/report/{token}/compare"
    return HTMLResponse(
        f"<html><body><div style='font-family:sans-serif;text-align:center;padding:60px'>"
        f"<h2>Schedule your call to view this report</h2>"
        f"<p style='color:#666;font-size:14px;margin:12px 0 20px'>The competitive comparison unlocks once you book your discovery call.</p>"
        f"<a href='{_esc(compare_url)}' style='display:inline-block;background:#E65100;color:white;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600'>Schedule Now</a>"
        f"</div></body></html>"
    )


def _render_generating_page(token: str, company_name: str) -> str:
    """Branded 'still generating' page that polls /booking-status every 4s
    and reloads the moment competitor_html is ready. Auto-email also fires
    on completion, so even if the user closes this tab, they get the link
    in their inbox."""
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Generating Comparison — {_esc(company_name)}</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, sans-serif; background: #f5f7f5; color: #1a1a1a; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }}
    .card {{ background: white; border-radius: 12px; padding: 48px 40px; max-width: 520px; width: 100%; text-align: center; box-shadow: 0 4px 24px rgba(0,0,0,0.08); }}
    .card img {{ width: 200px; margin-bottom: 24px; }}
    .spinner {{ display: inline-block; width: 56px; height: 56px; border: 5px solid #eaf3ea; border-top-color: #1B5E20; border-radius: 50%; animation: spin 1s linear infinite; margin-bottom: 24px; }}
    .card h1 {{ color: #1B5E20; font-size: 22px; margin-bottom: 12px; }}
    .card p {{ color: #555; font-size: 14px; line-height: 1.6; margin-bottom: 8px; }}
    .countdown {{ display: inline-block; margin-top: 16px; padding: 8px 16px; background: #f5f7f5; border-radius: 6px; font-size: 13px; color: #555; font-variant-numeric: tabular-nums; }}
    .countdown b {{ color: #1B5E20; }}
    .footer {{ margin-top: 24px; font-size: 12px; color: #888; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style>
</head><body>
<div class="card">
    <img src="https://backyardmarketingpros.com/wp-content/uploads/2024/09/BMP_Logo_Color_Horiz-1024x269.png" alt="BMP">
    <div class="spinner"></div>
    <h1>Building your competitive comparison</h1>
    <p>Auditing the top businesses in <strong>{_esc(company_name)}</strong>'s market right now.</p>
    <p>This usually takes about <strong>60 seconds</strong> — hold tight.</p>
    <div class="countdown" id="countdown">Elapsed: <b id="elapsed">0s</b></div>
    <div class="footer">We'll also email this to you when it's ready, so you can close this tab if you need to.</div>
</div>
<script>
const startedAt = Date.now();
const elapsedEl = document.getElementById('elapsed');

function tickElapsed() {{
    const sec = Math.floor((Date.now() - startedAt) / 1000);
    elapsedEl.textContent = sec + 's';
}}
setInterval(tickElapsed, 1000);

let pollCount = 0;
const POLL_INTERVAL_MS = 4000;
const MAX_POLLS = 75;  // 5 minutes — generation runs <90s in practice; long tail covered by email
async function poll() {{
    pollCount++;
    try {{
        const res = await fetch('/api/report/{token}/booking-status', {{ cache: 'no-store' }});
        const data = await res.json();
        if (data && data.generated) {{
            window.location.reload();
            return;
        }}
    }} catch(e) {{ /* network blip; keep polling */ }}
    if (pollCount < MAX_POLLS) {{
        setTimeout(poll, POLL_INTERVAL_MS);
    }} else {{
        // After 5 min, surface a friendly fallback message
        document.querySelector('.card h1').textContent = "Taking longer than expected";
        document.querySelector('.card p').textContent = "We'll email you the report as soon as it's ready — usually within a few minutes.";
    }}
}}
setTimeout(poll, POLL_INTERVAL_MS);
</script>
</body></html>"""


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

    # NOTE: report generation is now gated behind the iClosed booking webhook
    # (see iclosed_webhook below). Generating here would spend DataForSEO
    # credits on prospects who never schedule. Generation kicks off the moment
    # iClosed confirms a real booking, and the polling JS on this gate page
    # auto-redirects to /competitors once generation completes.

    public_url = settings.public_url.rstrip("/")
    competitors_url = f"{public_url}/report/{token}/competitors"

    company_name = company.name if company else "Your Business"
    booking_url = settings.iclosed_booking_url

    # Gated page: blurred preview + iClosed widget. The iClosed webhook
    # is the source of truth for "booked"; we poll /booking-status every
    # few seconds and auto-redirect to the unlocked report once the
    # webhook fires. No duplicate email form.
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Competitive Comparison — {_esc(company_name)}</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, sans-serif; background: #f5f7f5; color: #1a1a1a; }}
    .container {{ max-width: 820px; margin: 0 auto; padding: 20px; }}
    .header {{ background: linear-gradient(135deg, #0D3B13, #1B5E20); color: white; border-radius: 12px; padding: 32px; text-align: center; margin-bottom: 24px; }}
    .header img {{ width: 200px; margin-bottom: 12px; }}
    .blurred {{ filter: blur(8px); pointer-events: none; user-select: none; opacity: 0.6; padding: 20px; background: white; border-radius: 12px; margin-bottom: 24px; }}
    .blurred table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    .blurred td, .blurred th {{ padding: 8px; border-bottom: 1px solid #eee; }}
    .gate {{ background: white; border-radius: 12px; padding: 0; box-shadow: 0 4px 24px rgba(0,0,0,0.08); overflow: hidden; }}
    .gate-header {{ padding: 24px 24px 16px 24px; }}
    .gate-header h2 {{ color: #1B5E20; margin-bottom: 4px; text-align: center; }}
    .gate-header p.lede {{ color: #666; font-size: 14px; text-align: center; margin: 0; }}
    .iclosed-frame {{ width: 100%; height: 1300px; border: 0; display: block; background: transparent; }}
    .status-row {{ padding: 14px 24px 20px; text-align: center; font-size: 13px; color: #666; border-top: 1px solid #f0f0f0; }}
    .status-row .pulse {{ display: inline-block; width: 8px; height: 8px; background: #FF723F; border-radius: 50%; margin-right: 6px; animation: pulse 1.5s ease-in-out infinite; vertical-align: middle; }}
    .escape-link {{ display: block; margin-top: 8px; font-size: 12px; color: #888; text-decoration: underline; cursor: pointer; }}
    .escape-link:hover {{ color: #555; }}
    .success {{ display: none; text-align: center; padding: 40px 20px; background: white; border-radius: 12px; }}
    .success h2 {{ color: #1B5E20; margin-bottom: 12px; }}
    @keyframes pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.3; }} }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
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

    <!-- Gate: iClosed booking widget. Scheduling IS the gate.
         The iframe runs edge-to-edge inside the white card so the page
         reads as one unified booking surface (header → widget → status)
         instead of looking like a frame nested in a frame. -->
    <div class="gate" id="gate-form">
        <div class="gate-header">
            <h2>Schedule a quick 15-minute call to unlock</h2>
            <p class="lede">Pick a time below — we'll walk through where you're winning, where competitors are pulling ahead, and the fastest fixes.</p>
        </div>
        <iframe class="iclosed-frame"
                src="{booking_url}"
                allow="fullscreen *"
                loading="lazy"></iframe>
        <div class="status-row">
            <span class="pulse"></span>
            <span id="status-text">Once you book, we'll automatically unlock your comparison report.</span>
            <a class="escape-link" id="escape-link" onclick="manualUnlock()">Already booked? Click here to view your report →</a>
        </div>
    </div>

    <!-- Success / redirect -->
    <div class="success" id="gate-success">
        <h2>You're booked! 🎉</h2>
        <p style="color:#555">Unlocking your competitive comparison...</p>
        <div style="margin:16px auto;width:40px;height:40px;border:4px solid #ddd;border-top-color:#1B5E20;border-radius:50%;animation:spin 1s linear infinite"></div>
    </div>
</div>

<script>
let pollCount = 0;
const POLL_INTERVAL_MS = 4000;
const MAX_POLLS = 150;  // ~10 minutes — plenty for someone to fill the form + pick a slot
let pollTimer = null;

async function checkBooking() {{
    pollCount++;
    try {{
        const res = await fetch('/api/report/{token}/booking-status', {{ cache: 'no-store' }});
        const data = await res.json();
        if (data && data.booked) {{
            showUnlock();
            return;
        }}
    }} catch(e) {{ /* network blip; keep polling */ }}
    if (pollCount < MAX_POLLS) {{
        pollTimer = setTimeout(checkBooking, POLL_INTERVAL_MS);
    }}
}}

function showUnlock() {{
    if (pollTimer) clearTimeout(pollTimer);
    document.getElementById('gate-form').style.display = 'none';
    document.getElementById('gate-success').style.display = 'block';
    setTimeout(() => {{ window.location.href = '{competitors_url}'; }}, 1200);
}}

// Manual escape hatch — for the rare case the webhook didn't match
// (e.g. they used a different email than the contact has on file).
async function manualUnlock() {{
    if (!confirm('Confirm: you have already scheduled your discovery call?')) return;
    try {{
        await fetch('/api/report/{token}/unlock', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ email: '', token: '{token}' }}),
        }});
    }} catch(e) {{ /* fall through */ }}
    showUnlock();
}}

// Listen for postMessage from the iClosed iframe — if iClosed sends a
// "booking-confirmed" event we redirect immediately instead of waiting
// for the next poll. Best-effort; the polling path remains the canonical
// signal because the webhook is server-authoritative.
window.addEventListener('message', (ev) => {{
    if (!ev.data) return;
    const txt = JSON.stringify(ev.data).toLowerCase();
    if (txt.includes('booking') && (txt.includes('confirm') || txt.includes('success') || txt.includes('booked'))) {{
        // Trigger an immediate poll instead of trusting the postMessage payload
        // outright — server-side webhook is the truth.
        checkBooking();
    }}
}});

// Kick off polling shortly after page load so we're not hammering the server
// while they're still typing in the iframe form.
setTimeout(checkBooking, POLL_INTERVAL_MS);
</script>
</body></html>""")


# ============================================================
# Booking-status poll — called every few seconds by the gate page
# ============================================================

@router.get("/api/report/{token}/booking-status")
async def report_booking_status(token: str, db: AsyncSession = Depends(get_db)):
    """Status snapshot used by both the gate page and the post-booking
    'still generating' page.

    - `booked`     — iClosed webhook fired (audit_reports.booked_at set)
    - `generated`  — competitor comparison HTML is ready in DB

    Gate page polls until booked=true → redirect to /competitors.
    /competitors poll page polls until generated=true → reload to view.
    """
    report = (await db.execute(
        select(AuditReportModel).where(AuditReportModel.token == token)
    )).scalar_one_or_none()
    if not report:
        return {"booked": False, "generated": False, "found": False}
    return {
        "booked": bool(report.booked_at),
        "generated": bool(report.competitor_html),
        "found": True,
    }


# ============================================================
# Unlock endpoint — manual fallback when the webhook didn't match
# ============================================================

from pydantic import BaseModel as _BM


class UnlockRequest(_BM):
    email: str
    token: str


@router.post("/api/report/{token}/unlock")
async def unlock_competitor_report(
    token: str,
    req: UnlockRequest,
    db: AsyncSession = Depends(get_db),
):
    """Prospect clicked "I've scheduled" below the iClosed widget.

    iClosed handles the actual booking inside its iframe — name, email,
    phone, time slot. This endpoint records the self-confirmation, mirrors
    the email into the CRM contact if missing, and creates a BDR task.

    A separate /api/iclosed/webhook endpoint (see below) authoritatively
    flips report.booked_at when iClosed posts a confirmed booking event.
    Here we only capture the email — never claim a meeting was actually
    booked unless the webhook fires.
    """
    report = (await db.execute(
        select(AuditReportModel).where(AuditReportModel.token == token)
    )).scalar_one_or_none()

    if not report:
        return {"success": False, "error": "Report not found"}

    company = (await db.execute(
        select(Company).where(Company.id == report.company_id)
    )).scalar_one_or_none()

    # Mirror email into CRM contact if missing
    from app.models import Contact
    if company and req.email:
        contacts = (await db.execute(
            select(Contact).where(Contact.company_id == company.id)
            .order_by(Contact.is_primary.desc())
        )).scalars().all()
        if contacts and not contacts[0].email:
            contacts[0].email = req.email

    if company:
        db.add(Activity(
            company_id=company.id,
            activity_type="report_unlock_clicked",
            content=f"Prospect clicked 'I've Scheduled' on competitor gate (email: {req.email}). "
                    f"Awaiting iClosed webhook to confirm a real booking.",
        ))
        # Hot-lead task for BDR — even if booking can't be verified yet,
        # the click on the schedule-confirm button is itself a strong signal.
        if company.assigned_to:
            db.add(Task(
                company_id=company.id,
                user_id=company.assigned_to,
                description=f"HOT: {company.name} self-confirmed scheduling on competitor report "
                            f"(email: {req.email}) — verify booking landed in iClosed and call to prep.",
                due_date=datetime.now(timezone.utc),
            ))
        if company.status in ("new", "pursuing", "sequencing", "contacted"):
            company.status = "qualified"

    await db.commit()
    return {"success": True}


# ============================================================
# iClosed webhook — authoritative source-of-truth for "booked"
# ============================================================

@router.post("/api/iclosed/webhook")
async def iclosed_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Receive booking-confirmed events from iClosed.

    Configure in iClosed settings:
      Webhook URL:  {public_url}/api/iclosed/webhook?t=<iclosed_webhook_secret>
      Events:       booking.created (or whatever iClosed names it)

    Auth: shared-secret token via ?t=<secret> query param. When
    settings.iclosed_webhook_secret is empty, no check is performed (dev
    convenience). In prod, set ICLOSED_WEBHOOK_SECRET in .env and append
    ?t=<value> to the webhook URL inside iClosed.

    Match strategy: iClosed posts the booked email + scheduled time. We
    look up the most recent AuditReport whose booked_email matches (set
    by /unlock above) OR whose company has a contact with that email.
    First match wins.

    Logs a 'meeting_booked' Activity, creates a BDR task with the actual
    time, advances any qualified deals, and stamps report.booked_at.
    """
    # Shared-secret guard. Defense in depth — even if the URL leaks via
    # browser history / referrer / accidental commit, the secret is what
    # makes spoofed bookings non-trivial.
    expected_secret = (settings.iclosed_webhook_secret or "").strip()
    if expected_secret:
        provided = (request.query_params.get("t") or "").strip()
        if not _const_eq(provided, expected_secret):
            from fastapi import HTTPException as _HE
            raise _HE(status_code=401, detail="bad webhook token")

    import json as _json
    raw = await request.body()
    try:
        data = _json.loads(raw or b"{}")
    except Exception:
        return {"ok": False, "error": "invalid json"}

    # iClosed payload shape varies by event type; this is best-effort.
    # Payload fields are guarded so a schema change doesn't 500 the webhook.
    event = data.get("event") or data.get("type") or ""
    booking = data.get("data") or data.get("booking") or data
    booked_email = (booking.get("inviteeEmail") or booking.get("email") or "").strip().lower()
    scheduled_at = booking.get("startTime") or booking.get("scheduledAt") or ""
    invitee_name = booking.get("inviteeFirstName") or booking.get("name") or ""

    if not booked_email:
        return {"ok": True, "ignored": "no email in payload"}

    # Find the most recent report whose booked_email matches OR whose
    # company has a contact with this email. Prefer reports already touched
    # by the self-confirm /unlock click (booked_email set, booked_at null).
    from app.models import Contact
    report = (await db.execute(
        select(AuditReportModel)
        .where(AuditReportModel.booked_email == booked_email)
        .order_by(AuditReportModel.id.desc())
        .limit(1)
    )).scalar_one_or_none()

    if not report:
        # Fallback: match by contact email → company → most-recent report
        contact = (await db.execute(
            select(Contact).where(Contact.email == booked_email).order_by(Contact.id.desc()).limit(1)
        )).scalar_one_or_none()
        if contact:
            report = (await db.execute(
                select(AuditReportModel)
                .where(AuditReportModel.company_id == contact.company_id)
                .order_by(AuditReportModel.id.desc())
                .limit(1)
            )).scalar_one_or_none()

    if not report:
        return {"ok": True, "ignored": "no matching report", "email": booked_email}

    report.booked_at = datetime.now(timezone.utc)
    if not report.booked_email:
        report.booked_email = booked_email

    company = (await db.execute(
        select(Company).where(Company.id == report.company_id)
    )).scalar_one_or_none()
    if company:
        when = f" for {scheduled_at}" if scheduled_at else ""
        db.add(Activity(
            company_id=company.id,
            activity_type="meeting_booked",
            content=f"iClosed confirmed booking: {invitee_name or booked_email}{when} "
                    f"(via competitor report gate)",
        ))
        if company.assigned_to:
            db.add(Task(
                company_id=company.id,
                user_id=company.assigned_to,
                description=f"MEETING BOOKED: {invitee_name or booked_email} from {company.name}"
                            f"{when} — call to prep!",
                due_date=datetime.now(timezone.utc),
            ))
        # Advance any in-flight deal to qualified
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
        if company.status in ("new", "pursuing", "sequencing", "contacted"):
            company.status = "qualified"

    await db.commit()

    # Now that the booking is confirmed, kick off the competitor report
    # generation. We deliberately don't generate before this point so we
    # don't burn DataForSEO credits on prospects who never schedule.
    # Generation is fire-and-forget; when it completes, the auto-email
    # path inside _generate_competitor_report_bg sends the report link to
    # the booked address and logs a CRM Activity.
    if not report.competitor_html:
        import asyncio as _asyncio
        _asyncio.create_task(_generate_competitor_report_bg(report.id))

    # Outbound webhook to any customer endpoints subscribed to meeting.booked
    try:
        from app.services.webhook_dispatch import dispatch_event
        await dispatch_event(db, "meeting.booked", {
            "report_id": report.id,
            "company_id": company.id if company else None,
            "company_name": company.name if company else None,
            "booked_email": booked_email,
            "invitee_name": invitee_name,
            "scheduled_at": scheduled_at,
            "source": "iclosed",
        })
    except Exception:
        pass

    return {"ok": True, "report_id": report.id, "matched_email": booked_email}


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

                # Auto-email the report to the booked address. We do this
                # after the commit so a failed email never rolls back the
                # generation work — the BDR can resend manually if needed.
                if report.booked_email:
                    try:
                        await _email_competitor_report(
                            db, report=report, company=company,
                            to_email=report.booked_email,
                        )
                    except Exception as e:
                        log.exception(f"Auto-email of competitor report failed: {e}")
            else:
                log.warning(f"No competitors found for {company.name} in SERP")

    except Exception as e:
        import logging
        logging.getLogger("bmp").exception(f"Background competitor report failed: {e}")


async def _email_competitor_report(db, *, report, company, to_email: str) -> None:
    """Send the booked prospect a short email with a link to their report.

    Uses the company's assigned BDR as the from-line so reply tracking
    points at a real person on the team. Falls back to the first sending-
    enabled admin if no assignee is set.
    """
    from app.services.email_sender import send_email, get_sender_info
    from app.services.signature import render_signature

    if not settings.resend_api_key or not to_email:
        return

    sender_user = None
    if company.assigned_to:
        sender_user = (await db.execute(
            select(User).where(User.id == company.assigned_to)
        )).scalar_one_or_none()
    if not sender_user or not sender_user.sending_enabled:
        sender_user = (await db.execute(
            select(User).where(
                User.role.in_(("admin", "super_admin")),
                User.sending_enabled == True,
            )
        )).scalars().first()
    if not sender_user:
        return  # No one available to send from — silent skip

    sender = get_sender_info(sender_user.first_name, sender_user.full_name)
    public_url = settings.public_url.rstrip("/")
    report_url = f"{public_url}/report/{report.token}/competitors"
    company_name = company.name or "your business"

    subject = f"Your Competitive Comparison for {company_name} is ready"
    body = (
        f"Hi,\n\n"
        f"Your competitive comparison report for {company_name} is ready. "
        f"We audited the top businesses in your market and put it side-by-side "
        f"with your AI findability, content, and local SEO scores so you can see "
        f"exactly where the gaps are.\n\n"
        f'<a href="{report_url}" style="display:inline-block;background:#E65100;color:white;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;font-size:15px;margin:8px 0">View My Comparison Report</a>\n\n'
        f"Take a look before our call so we can dig into the biggest opportunities together.\n\n"
        f"— {sender_user.first_name}"
    )

    import json
    sig_html = render_signature(sender_user)
    result = await send_email(
        to_email=to_email,
        subject=subject,
        body=body,
        from_name=sender["from_name"],
        from_firstname=sender["from_firstname"],
        reply_to_email=sender_user.email,  # direct reply to BDR
        company_id=company.id,
        contact_id=0,
        email_id=0,  # not part of a sequence
        signature_html=sig_html,
        unsubscribe_token=None,  # transactional follow-up, not outreach
    )

    if result.get("success"):
        # Log it to the company timeline so the BDR sees the auto-send
        from app.services.credit_meter import meter, make_idem_key
        db.add(Activity(
            company_id=company.id,
            user_id=sender_user.id,
            activity_type="competitor_report_sent",
            content=f"📤 Competitor report auto-emailed to {to_email}",
            metadata_json=json.dumps({
                "to": to_email,
                "report_url": report_url,
                "resend_id": result.get("resend_id"),
            }),
        ))
        await meter(
            db, action_type="email_send",
            idempotency_key=make_idem_key("email_send", "competitor_report", report.id),
            user_id=sender_user.id,
            action_ref=f"competitor_report:{report.id}",
        )
        await db.commit()
