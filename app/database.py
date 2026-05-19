from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import settings

# Dialect-aware engine config. SQLite gets concurrency hardening
# (WAL mode + busy_timeout); Postgres gets connection pooling sized
# for a typical async FastAPI app (10 base + 5 overflow = 15 max).
_connect_args = {}
_engine_kwargs: dict = {"echo": False}

if settings.database_url.startswith("sqlite"):
    # busy_timeout (in seconds) makes writers wait briefly for a lock
    # instead of failing with "database is locked". 30s matches the
    # Django/Rails default; gives WAL log time to drain under burst.
    # check_same_thread=False is required for async coroutine access.
    _connect_args = {"timeout": 30, "check_same_thread": False}
elif "postgresql" in settings.database_url:
    # Postgres pool sizing — modest defaults that work for a single
    # uvicorn worker. Bump pool_size if we move to multi-worker.
    # pool_pre_ping is intentionally OFF: it fires a SELECT 1 on every
    # checkout, which doubled per-query latency against Supabase direct
    # (measured ~240ms overhead on 2026-05-12). pool_recycle keeps
    # connections fresh enough that stale-conn errors are rare; when one
    # slips through, SQLAlchemy reconnects on next request.
    _engine_kwargs["pool_size"] = 10
    _engine_kwargs["max_overflow"] = 5
    _engine_kwargs["pool_recycle"] = 1800  # recycle conns every 30 min

engine = create_async_engine(
    settings.database_url,
    connect_args=_connect_args,
    **_engine_kwargs,
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session


async def init_db():
    """Create new tables (idempotent) + run column-add migrations.

    SQLAlchemy's create_all only creates tables that don't exist; it does
    NOT alter existing tables to add new columns. For column-additions we
    have idempotent migrate_*.py scripts. Most are wired into systemd
    ExecStartPre on the VPS, but recent ones get chained here too so a
    fresh checkout works without operator intervention. Each migration is
    idempotent (PRAGMA table_info check before ALTER), so running them
    twice is harmless.
    """
    # SQLite concurrency: switch the database file to WAL mode (one writer
    # but unblocked readers — sane behavior under any load) and lower the
    # synchronous level to NORMAL (still durable; faster commits when
    # paired with WAL). These are persistent file-level settings; once
    # applied they stick across processes / restarts.
    if settings.database_url.startswith("sqlite"):
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA synchronous=NORMAL"))

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Recent column-additions — chained here as a safety net even when
    # the VPS systemd unit also runs them.
    for _migration_module in (
        "scripts.migrate_audit_booked",
        "scripts.migrate_reply_sentiment",
        "scripts.migrate_apollo_key",
        "scripts.migrate_lead_score",
        "scripts.migrate_campaign_targets",
        "scripts.migrate_custom_fields",
        "scripts.migrate_company_socials",  # ordering matters: must run AFTER migrate_custom_fields
        "scripts.migrate_morning_brief",
        "scripts.migrate_sos_lookups",
        "scripts.migrate_netrows_extras",
        "scripts.migrate_contact_linkedin_profile",
        "scripts.migrate_audit_log",
        "scripts.migrate_api_keys_webhooks",
        "scripts.migrate_zoominfo",
        "scripts.migrate_tier2_netrows",
        "scripts.migrate_google_oauth",
        "scripts.migrate_scheduler",
        "scripts.migrate_google_maps_key",
        "scripts.migrate_scheduler_v2",
        "scripts.migrate_scheduler_branding",
        "scripts.migrate_apikey_scope",
        "scripts.migrate_audit_branding",
        "scripts.migrate_audit_layout",
        "scripts.migrate_org_brand",
        "scripts.migrate_email_events",
        "scripts.migrate_brand_website",
        "scripts.migrate_missive_link",
        "scripts.migrate_pipeline_stages",
        "scripts.migrate_autopilot_window",
        "scripts.migrate_autopilot_per_channel",
        "scripts.migrate_booking_routing",
        "scripts.migrate_call_diarization",
        "scripts.migrate_site_visitor_sessions",
        "scripts.migrate_campaign_archive",
        "scripts.migrate_performance_indexes",
        "scripts.migrate_sequence_templates",
    ):
        try:
            mod = __import__(_migration_module, fromlist=["main"])
            await mod.main()
        except Exception:
            # Migrations log their own outcome; don't crash startup if one
            # fails — the app should still come up so the operator can debug.
            pass
