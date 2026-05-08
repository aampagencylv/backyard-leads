import asyncio
import logging
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
)


log = logging.getLogger("bmp")


async def _sequence_engine_loop():
    """Background tick: run the sequence engine every 60s + check snoozed
    deals every 10 min + run the morning-brief tick every 15 min."""
    from app.services.sequence_engine import process_pending_steps
    tick_count = 0
    while True:
        try:
            async with async_session() as db:
                counters = await process_pending_steps(db)
            if any(counters[k] for k in ("sent", "skipped", "tasks_created", "errors")):
                log.info(f"sequence_engine tick: {counters}")
        except Exception as e:
            log.exception(f"sequence_engine tick failed: {e}")

        # Snoozed-deal wake check every 10 ticks (10 min)
        tick_count += 1
        if tick_count % 10 == 0:
            try:
                await _wake_snoozed_deals()
            except Exception as e:
                log.exception(f"snooze wake check failed: {e}")

        # Morning-brief tick every 15 ticks (15 min). The tick itself
        # iterates active users and only sends to those whose local time
        # has just crossed their configured brief_hour.
        if tick_count % 15 == 0:
            try:
                from app.services.morning_brief import run_morning_brief_tick
                async with async_session() as db:
                    sent_count = await run_morning_brief_tick(db)
                if sent_count:
                    log.info(f"morning_brief tick: {sent_count} brief(s) sent")
            except Exception as e:
                log.exception(f"morning_brief tick failed: {e}")

        await asyncio.sleep(60)


async def _wake_snoozed_deals():
    """Auto-wake deals whose snooze date has passed. Creates follow-up tasks."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.models import Deal, Activity, Task, Company

    async with async_session() as db:
        now = datetime.now(timezone.utc)
        deals = (await db.execute(
            select(Deal).where(
                Deal.stage == "snoozed",
                Deal.snoozed_until.isnot(None),
                Deal.snoozed_until <= now,
            )
        )).scalars().all()

        for deal in deals:
            restore = deal.stage_before_snooze or "prospecting"
            from app.routes.deal_routes import STAGE_PROBABILITY, package_monthly_value
            deal.stage = restore
            deal.probability = STAGE_PROBABILITY.get(restore, 10)
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

# CORS: restrict to our own surfaces. Auth uses Bearer tokens in localStorage,
# not cookies, so we don't need allow_credentials. The only cross-origin caller
# is the bymp.com WVT snippet hitting /api/track/pageview — bymp.com is in the
# allow list. Inbound webhooks (Blooio / Resend / Twilio) are server-to-server
# and don't go through CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://prospector.backyardmarketingpros.com",
        "https://backyardmarketingpros.com",
        "https://www.backyardmarketingpros.com",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
)

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

# Serve static frontend
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def serve_app():
    with open("static/index.html") as f:
        html = f.read()
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/health")
async def health():
    return {"status": "ok", "app": "Backyard Leads"}
