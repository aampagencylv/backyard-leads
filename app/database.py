from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import settings

engine = create_async_engine(settings.database_url, echo=False)
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
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Recent column-additions — chained here as a safety net even when
    # the VPS systemd unit also runs them.
    for _migration_module in (
        "scripts.migrate_audit_booked",
        "scripts.migrate_reply_sentiment",
        "scripts.migrate_apollo_key",
    ):
        try:
            mod = __import__(_migration_module, fromlist=["main"])
            await mod.main()
        except Exception:
            # Migrations log their own outcome; don't crash startup if one
            # fails — the app should still come up so the operator can debug.
            pass
