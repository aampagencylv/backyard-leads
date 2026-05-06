from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.database import init_db
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
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


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
