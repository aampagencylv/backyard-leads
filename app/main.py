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
)


log = logging.getLogger("bmp")


async def _sequence_engine_loop():
    """Background tick: run the sequence engine every 60s. Catches its own
    exceptions so a transient DB error doesn't kill the loop."""
    from app.services.sequence_engine import process_pending_steps
    while True:
        try:
            async with async_session() as db:
                counters = await process_pending_steps(db)
            if any(counters[k] for k in ("sent", "skipped", "tasks_created", "errors")):
                log.info(f"sequence_engine tick: {counters}")
        except Exception as e:
            log.exception(f"sequence_engine tick failed: {e}")
        await asyncio.sleep(60)


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
