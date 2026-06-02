import asyncio
import logging
# Configure observability before importing anything else that logs.
# This way the very first log lines (route registration, etc.) carry
# our format + rid placeholder.
from app.observability import configure_logging, configure_sentry
configure_logging()
configure_sentry()  # no-op when SENTRY_DSN unset

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.database import init_db, async_session
from app.routes import (
    auth_routes,
    search_routes,
    company_routes,
    contact_routes,
    deal_routes,
    send_routes,
    crm_routes,
    dashboard_routes,
    runtime_routes,
    unsubscribe_routes,
    view_routes,
    campaign_routes,
    twilio_routes,
    blooio_routes,
    sequence_routes,
    tracking_routes,
    notification_routes,
    audit_routes,
    email_inbound_routes,
    credit_routes,
    custom_field_routes,
    public_api,
    integration_routes,
    google_oauth_routes,
    scheduler_routes,
    upload_routes,
    mcp_routes,
    ai_chat_routes,
    dns_health_routes,
    reputation_routes,
    integrations_context_routes,
    missive_routes,
    embed_sidebar_routes,
    extension_download_routes,
    feedback_routes,
    site_visitors_routes,
    sequence_template_routes,
    admin_routes,
    onboard_routes,
)


log = logging.getLogger("bmp")


async def _list_active_tenant_ids() -> list[int]:
    """Return ids of all tenants with status='active'.

    Cross-tenant lookup — the session is not scoped, so the ORM
    auto-filter doesn't apply (Tenant model itself isn't a TenantMixin).
    """
    from sqlalchemy import select as _select
    from app.models import Tenant as _Tenant
    async with async_session() as db:
        rows = (await db.execute(
            _select(_Tenant.id).where(_Tenant.status == "active")
        )).scalars().all()
    return list(rows) or [1]  # fallback to BMP if the tenants table is empty


async def _sequence_engine_loop():
    """Background tick. Every 60s, per active tenant:
       - sequence engine (fire ready email/iMessage steps)
       - every 5 min: advance any running full_auto campaigns by one batch
       - every 10 min: wake snoozed deals
       - every 15 min: morning brief check

    Every per-tenant pass runs inside `tenant_scope(db, tid)`, which
    activates the ORM auto-filter. Per-tenant processing means a new
    tenant's queue isn't touched until we reach their iteration this
    tick — fair across tenants, and no cross-tenant data leak.
    """
    from app.services.sequence_engine import process_pending_steps
    from app.tenancy import tenant_scope
    tick_count = 0
    while True:
        try:
            tenant_ids = await _list_active_tenant_ids()
        except Exception as e:
            log.exception(f"tenant enumeration failed: {e}")
            tenant_ids = [1]  # don't lose a tick on a transient lookup error

        for tid in tenant_ids:
            try:
                async with async_session() as db:
                    with tenant_scope(db, tid):
                        try:
                            counters = await process_pending_steps(db)
                        except Exception:
                            await db.rollback()
                            raise
                if any(counters[k] for k in ("sent", "skipped", "tasks_created", "errors")):
                    log.info(f"sequence_engine tick (tenant={tid}): {counters}")
            except Exception as e:
                log.exception(f"sequence_engine tick failed (tenant={tid}): {e}")

        tick_count += 1

        # Scheduled-campaign activation — every tick (60s) per tenant.
        for tid in tenant_ids:
            try:
                await _activate_scheduled_campaigns(tid)
            except Exception as e:
                log.exception(f"scheduled-campaign activation failed (tenant={tid}): {e}")

        # Campaign auto-advance — every tick (60s) per tenant. Each batch
        # takes 1-2 min internally (Google Maps + enrichment + Claude email
        # generation), so back-to-back firing produces continuous
        # throughput rather than artificially throttled spacing.
        for tid in tenant_ids:
            try:
                await _advance_full_auto_campaigns(tid)
            except Exception as e:
                log.exception(f"campaign auto-advance failed (tenant={tid}): {e}")

        # Call reconciliation every 5 ticks (5 min). Per tenant.
        # 24h lookback is idempotent on twilio_call_sid (cheap to re-scan).
        if tick_count % 5 == 0:
            from app.services.call_reconciliation import reconcile_calls
            for tid in tenant_ids:
                try:
                    async with async_session() as db:
                        with tenant_scope(db, tid):
                            rc = await reconcile_calls(db, hours=24)
                    if rc.get("stubs_created"):
                        log.info(f"call_recon tick (tenant={tid}): {rc}")
                except Exception as e:
                    log.exception(f"call_recon tick failed (tenant={tid}): {e}")

        # Snoozed-deal wake check every 10 ticks (10 min) per tenant.
        if tick_count % 10 == 0:
            for tid in tenant_ids:
                try:
                    await _wake_snoozed_deals(tid)
                except Exception as e:
                    log.exception(f"snooze wake check failed (tenant={tid}): {e}")

        # Morning-brief tick every 15 ticks (15 min) per tenant.
        if tick_count % 15 == 0:
            from app.services.morning_brief import run_morning_brief_tick
            for tid in tenant_ids:
                try:
                    async with async_session() as db:
                        with tenant_scope(db, tid):
                            sent_count = await run_morning_brief_tick(db)
                    if sent_count:
                        log.info(f"morning_brief tick (tenant={tid}): {sent_count} brief(s) sent")
                except Exception as e:
                    log.exception(f"morning_brief tick failed (tenant={tid}): {e}")

        await asyncio.sleep(60)


async def _activate_scheduled_campaigns(tenant_id: int):
    """Flip 'scheduled' campaigns to 'running' once their start time passes.
    Scoped to the given tenant via the ORM auto-filter."""
    from datetime import datetime as _dt, timezone as _tz
    from sqlalchemy import select as _select
    from app.models import Campaign as _Campaign
    from app.tenancy import tenant_scope

    now = _dt.now(_tz.utc)
    async with async_session() as db:
        with tenant_scope(db, tenant_id):
            due = (await db.execute(
                _select(_Campaign).where(
                    _Campaign.status == "scheduled",
                    _Campaign.scheduled_start_at.isnot(None),
                    _Campaign.scheduled_start_at <= now,
                )
            )).scalars().all()
            for camp in due:
                camp.status = "running"
                log.info(f"campaign #{camp.id} '{camp.name}' activated on schedule "
                         f"(was due {camp.scheduled_start_at.isoformat()})")
            if due:
                await db.commit()


async def _advance_full_auto_campaigns(tenant_id: int):
    """Run one batch per active full_auto campaign for this tenant."""
    from sqlalchemy import select as _select
    from app.models import Campaign as _Campaign
    from app.routes.campaign_routes import _execute_batch
    from app.tenancy import tenant_scope

    async with async_session() as db:
        with tenant_scope(db, tenant_id):
            rows = (await db.execute(
                _select(_Campaign).where(
                    _Campaign.status == "running",
                    _Campaign.mode == "full_auto",
                )
            )).scalars().all()

    if not rows:
        return

    # Need a "system user" actor for activity logging. Pick the
    # campaign's creator — they're the one running it conceptually.
    from app.models import User as _User
    for camp in rows:
        try:
            async with async_session() as db:
                with tenant_scope(db, tenant_id):
                    actor = (await db.execute(
                        _select(_User).where(_User.id == camp.created_by)
                    )).scalar_one_or_none()
                    if not actor:
                        log.warning(f"campaign {camp.id} has no creator — skipping batch")
                        continue
                    result = await _execute_batch(camp.id, db, actor)
                    status = (result or {}).get("status", "ok")
                    # Log meaningful results only; "daily_cap_reached" /
                    # "completed" are normal terminal states.
                    if status in ("ok", "no_results"):
                        log.info(f"campaign #{camp.id} auto-batch: {status} · "
                                 f"new={result.get('new_companies', 0)} "
                                 f"qualified={result.get('qualified', 0)} "
                                 f"sequences={result.get('sequences_created', 0)}")
                    elif status == "completed":
                        log.info(f"campaign #{camp.id} auto-batch: COMPLETED")
                    elif status == "daily_cap_reached":
                        log.info(f"campaign #{camp.id} auto-batch: daily cap reached "
                                 f"({result.get('prospects_today')} of cap)")
        except Exception as e:
            log.exception(f"campaign #{camp.id} batch failed: {e}")


async def _wake_snoozed_deals(tenant_id: int):
    """Auto-wake deals (scoped to tenant) whose snooze date has passed."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.models import Deal, Activity, Task, Company
    from app.tenancy import tenant_scope

    async with async_session() as db:
        with tenant_scope(db, tenant_id):
            now = datetime.now(timezone.utc)
            deals = (await db.execute(
                select(Deal).where(
                    Deal.stage == "snoozed",
                    Deal.snoozed_until.isnot(None),
                    Deal.snoozed_until <= now,
                )
            )).scalars().all()

            from app.services import pipeline_config as _pc
            from app.routes.deal_routes import package_monthly_value
            for deal in deals:
                restore = deal.stage_before_snooze or "in_sequence"
                # If admin deleted the stage while this deal was asleep,
                # fall back to in_sequence rather than stranding it.
                if not await _pc.is_valid_stage(db, restore):
                    restore = "in_sequence"
                deal.stage = restore
                deal.probability = await _pc.get_stage_probability(db, restore)
                if deal.value == 0 and deal.package:
                    deal.value = package_monthly_value(deal.package)

                reason = deal.snooze_reason or "Scheduled follow-up"
                deal.snoozed_until = None
                deal.stage_before_snooze = None
                deal.snooze_reason = None

                company = (await db.execute(select(Company).where(Company.id == deal.company_id))).scalar_one_or_none()
                company_name = company.name if company else "Unknown"

                # Create follow-up task
                if deal.assigned_to:
                    db.add(Task(
                        company_id=deal.company_id,
                        user_id=deal.assigned_to,
                        description=f"FOLLOW UP: {company_name} — snoozed reason: {reason}",
                        due_date=now,
                    ))

                db.add(Activity(
                    company_id=deal.company_id, deal_id=deal.id,
                    activity_type="deal_woken",
                    content=f"Deal auto-reactivated from snooze — restored to {restore}. Reason was: {reason}",
                ))

                log.info(f"Woke snoozed deal {deal.id} for {company_name}")

            if deals:
                await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Install the global ORM auto-tenant-filter hook. Idempotent.
    # Sessions opened via get_db are unaffected; only sessions opened
    # via get_tenant_db (which stamps session.info["tenant_id"]) get
    # auto-scoped queries.
    from app.tenancy import install_tenant_filter
    install_tenant_filter()
    task = asyncio.create_task(_sequence_engine_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(
    title="Backyard Leads",
    description="Lead intelligence platform for Backyard Marketing Pros",
    lifespan=lifespan,
)

# Middleware ordering note: Starlette runs middleware in reverse-add
# order. We want, on each request:
#   1. RequestId/error-handler outermost (catches everything, including
#      panics raised by other middleware)
#   2. SecurityHeaders next (so headers are attached even on errors)
#   3. RateLimit before route handlers (cheap rejection)
#   4. CORS innermost
# Add them in that priority order; Starlette will execute outside-in.
from app.middleware import (
    RequestIdAndErrorHandler,
    SecurityHeadersMiddleware,
    RateLimitMiddleware,
)

# CORS: restrict to our own surfaces. Auth uses Bearer tokens in localStorage,
# not cookies, so we don't need allow_credentials. The only cross-origin caller
# is the bymp.com WVT snippet hitting /api/track/pageview — bymp.com is in the
# allow list. Inbound webhooks (Blooio / Resend / Twilio) are server-to-server
# and don't go through CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://prospector.backyardmarketingpros.com",
        "https://audit.backyardmarketingpros.com",
        "https://schedule.backyardmarketingpros.com",
        "https://backyardmarketingpros.com",
        "https://www.backyardmarketingpros.com",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With", "X-API-Key", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)
# Add hardening middleware *after* CORS in source order (Starlette runs
# them in reverse-registration order, so RateLimit fires first, then
# SecurityHeaders, then RequestIdAndErrorHandler outermost).
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIdAndErrorHandler)

# API routes
app.include_router(auth_routes.router)
app.include_router(search_routes.router)
app.include_router(company_routes.router)
app.include_router(contact_routes.router)
app.include_router(deal_routes.router)
app.include_router(send_routes.router)
app.include_router(crm_routes.router)
app.include_router(dashboard_routes.router)
app.include_router(runtime_routes.router)
app.include_router(unsubscribe_routes.router)
app.include_router(view_routes.router)
app.include_router(campaign_routes.router)
app.include_router(twilio_routes.router)
app.include_router(blooio_routes.router)
app.include_router(sequence_routes.router)
app.include_router(tracking_routes.router)
app.include_router(notification_routes.router)
app.include_router(audit_routes.router)
app.include_router(email_inbound_routes.router)
app.include_router(credit_routes.router)
app.include_router(custom_field_routes.router)
app.include_router(public_api.router)
app.include_router(integration_routes.router)
app.include_router(google_oauth_routes.router)
app.include_router(scheduler_routes.host_router)
app.include_router(scheduler_routes.public_router)
app.include_router(scheduler_routes.booking_page_router)
app.include_router(upload_routes.router)
app.include_router(mcp_routes.router)
app.include_router(ai_chat_routes.router)
app.include_router(dns_health_routes.router)
app.include_router(reputation_routes.router)
app.include_router(integrations_context_routes.router)
app.include_router(missive_routes.router)
app.include_router(embed_sidebar_routes.router)
app.include_router(extension_download_routes.router)
app.include_router(site_visitors_routes.router)
app.include_router(feedback_routes.router)
app.include_router(sequence_template_routes.router)
app.include_router(admin_routes.router)
app.include_router(onboard_routes.router)

# Serve static frontend + user-uploaded files (logos, etc.).
# var/uploads/ is gitignored and persists across deploys; ensure the
# directory exists before mounting so Starlette doesn't error at boot
# on a fresh checkout.
from app.services.uploads import ensure_upload_dirs
ensure_upload_dirs()
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="var/uploads"), name="uploads")


# Hostnames that show the public LeadProspector marketing landing page.
# Everything else falls through to the tenant app (BMP today; any tenant
# slug or custom domain tomorrow).
_LANDING_HOSTS = {"leadprospector.ai", "www.leadprospector.ai"}

# Hostnames that show the platform admin console. Convenience routing so
# `app.leadprospector.ai/` lands on the admin shell instead of the tenant
# app — separate URL surface for the operator's daily-driver.
_ADMIN_HOSTS = {"app.leadprospector.ai"}


@app.get("/", response_class=HTMLResponse)
async def serve_app(request: Request):
    """Root handler. Host header decides which shell we return.

    - leadprospector.ai / www.leadprospector.ai → marketing landing
    - app.leadprospector.ai                     → platform admin console
    - everything else (tenant slugs + custom    → tenant app
      domains + BMP legacy hosts)
    """
    host = (request.headers.get("host") or "").split(":", 1)[0].lower().strip()
    if host in _LANDING_HOSTS:
        path = "static/landing.html"
    elif host in _ADMIN_HOSTS:
        path = "static/admin.html"
    else:
        path = "static/index.html"
    with open(path) as f:
        html = f.read()
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/admin", response_class=HTMLResponse)
async def serve_admin():
    """Platform admin console (tenants, domains, impersonation).
    Auth is enforced by the API endpoints the page calls — page itself
    is a static shell and safe to serve unauthenticated.

    Reachable at https://app.leadprospector.ai/ (host-routed via /)
    or directly via /admin on any host."""
    with open("static/admin.html") as f:
        html = f.read()
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})


# ============================================================
# PWA — service worker + manifest must be served from root, not
# /static/, so the SW scope can be "/" (a SW served from /static/sw.js
# can only control /static/* — useless for our app shell).
# ============================================================

@app.get("/sw.js")
async def pwa_service_worker():
    from fastapi.responses import FileResponse
    return FileResponse(
        "static/sw.js",
        media_type="application/javascript",
        # The SW file itself must NEVER be cached aggressively — when
        # we ship a new version we need browsers to pick it up on the
        # very next page load. The SW content then handles cache-busting
        # for everything else via its own SW_VERSION constant.
        headers={"Cache-Control": "no-store, max-age=0", "Service-Worker-Allowed": "/"},
    )


@app.get("/manifest.webmanifest")
async def pwa_manifest():
    from fastapi.responses import FileResponse
    return FileResponse(
        "static/manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "app": "Backyard Leads"}
