"""Reusable test fixtures.

Spins up an in-memory SQLite database, applies the full schema, and
provides factory functions for the load-bearing entities (tenant, user,
company, contact, sequence step). All tests that need a DB go through
these fixtures so the test DB looks like a real BMP install.

Email sending is stubbed at the module level so tests can't accidentally
hit Resend even if a guard regressed.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker


# ============================================================
# In-memory SQLite database
# ============================================================
#
# Why SQLite for tests:
#   - Zero setup, runs anywhere
#   - Per-test isolation via in-memory mode + new engine per test
#   - 100x faster than postgres for unit-level tests
#
# Caveats:
#   - SQLite doesn't enforce JSONB-specific behaviors (we don't use any
#     PG-only features in the paths we test)
#   - No PARTITION BY, GIN indexes, etc. — we don't test those either
#
# For PG-specific integration tests later, we'll spin up a real postgres
# via testcontainers. For now, SQLite covers the dispatch logic, snooze
# state machine, send guards, and route handlers — which is where the
# Texas Remodel Team incident lived.


@pytest_asyncio.fixture
async def db_session():
    """Fresh in-memory SQLite + all tables + a session, per-test isolated."""
    # SQLite in-memory + StaticPool so the same connection is used across
    # the session's queries (in-memory DBs are per-connection).
    from sqlalchemy.pool import StaticPool
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Apply the full schema from the models. We use Base.metadata.create_all
    # since our migration scripts target PG-specific syntax (PARTIAL INDEX
    # WHERE, ALTER TABLE ... ADD COLUMN with IF NOT EXISTS quirks).
    from app.database import Base
    from app import models  # noqa: F401 — import to register all model classes

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


# ============================================================
# Entity factories
# ============================================================

async def make_tenant(db: AsyncSession, name: str = "TestCo", tenant_id: int = 1):
    """Create or get a tenant row. SQLite doesn't enforce FKs by default,
    but we still want the row to exist for joins."""
    from app.models import Tenant
    from sqlalchemy import select
    existing = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if existing:
        return existing
    t = Tenant(id=tenant_id, name=name, status="active", plan="standard")
    db.add(t)
    await db.flush()
    return t


async def make_user(
    db: AsyncSession, tenant_id: int = 1, role: str = "sales_rep",
    email: Optional[str] = None, first_name: str = "Test",
    user_id: Optional[int] = None,
):
    from app.models import User
    if email is None:
        email = f"{first_name.lower()}@test.example.com"
    u = User(
        id=user_id, tenant_id=tenant_id, role=role, email=email,
        first_name=first_name, last_name="User",
        is_active=True, sending_enabled=True,
        twilio_identity=f"bmp_user_{user_id or 0}",
        is_available_for_calls=True,
    )
    db.add(u)
    await db.flush()
    return u


async def make_company(
    db: AsyncSession, tenant_id: int = 1, name: str = "Test Pools, LLC",
    business_type: str = "pool builder", city: str = "Phoenix", state: str = "AZ",
    assigned_to: Optional[int] = None,
):
    from app.models import Company
    c = Company(
        tenant_id=tenant_id, name=name, business_type=business_type,
        city=city, state=state, status="sequencing",
        assigned_to=assigned_to,
        website=f"https://{name.lower().replace(' ', '').replace(',', '').replace('.', '')}.com",
        rating=4.9, review_count=42,
    )
    db.add(c)
    await db.flush()
    return c


async def make_contact(
    db: AsyncSession, company_id: int, tenant_id: int = 1,
    first_name: str = "Tim", last_name: str = "Fox",
    email: str = "tim@example.com", phone: Optional[str] = "+15555551234",
    is_primary: bool = True,
):
    from app.models import Contact
    ct = Contact(
        tenant_id=tenant_id, company_id=company_id,
        first_name=first_name, last_name=last_name,
        email=email, phone=phone,
        is_primary=is_primary, email_status="valid",
    )
    db.add(ct)
    await db.flush()
    return ct


async def make_step(
    db: AsyncSession, contact_id: int, company_id: int, tenant_id: int = 1,
    step_type: str = "email", email_type: str = "cold", sequence_order: int = 1,
    subject: str = "Real subject", body: str = "Hi there, this is a real email body that is plenty long enough to clear the ultra_short_body anomaly threshold and should score zero.",
    auto_execute: bool = True, scheduled_send_at: Optional[datetime] = None,
    is_sent: bool = False, skipped_at: Optional[datetime] = None,
):
    from app.models import GeneratedEmail
    ge = GeneratedEmail(
        tenant_id=tenant_id, company_id=company_id, contact_id=contact_id,
        step_type=step_type, email_type=email_type, sequence_order=sequence_order,
        subject=subject, body=body, auto_execute=auto_execute,
        scheduled_send_at=scheduled_send_at or (datetime.now(timezone.utc) - timedelta(minutes=5)),
        is_sent=is_sent, skipped_at=skipped_at,
    )
    db.add(ge)
    await db.flush()
    return ge


@pytest_asyncio.fixture
async def bmp_world(db_session):
    """Pre-populated world: 1 tenant, 1 BDR, 1 company, 1 contact with
    a 4-step sequence (email/linkedin/call/email) where the email at
    seq=1 is sent and the rest are pending. Mirrors a real Texas-Remodel-
    Team-shaped sequence for regression testing."""
    db = db_session
    await make_tenant(db, tenant_id=1)
    bdr = await make_user(db, role="sales_rep", first_name="Sebastian", user_id=5)
    co = await make_company(db, assigned_to=bdr.id)
    ct = await make_contact(db, company_id=co.id)
    # The exact sequence shape from the incident:
    s1 = await make_step(db, contact_id=ct.id, company_id=co.id, sequence_order=1,
                         step_type="email", email_type="cold",
                         subject="ChatGPT isn't recommending your patio services",
                         is_sent=True)
    s2 = await make_step(db, contact_id=ct.id, company_id=co.id, sequence_order=2,
                         step_type="linkedin", email_type="linkedin_connect",
                         subject="LinkedIn step 2",
                         body="Connect note (under 280 chars):\n\nHey Tim — saw your work.",
                         auto_execute=False)
    s3 = await make_step(db, contact_id=ct.id, company_id=co.id, sequence_order=3,
                         step_type="call", email_type="call_1",
                         subject="Call 3",
                         body="📞 (555) 555-1234\n\nCall talk track:\n- Hi Tim — from BMP.",
                         auto_execute=False)
    s4 = await make_step(db, contact_id=ct.id, company_id=co.id, sequence_order=4,
                         step_type="email", email_type="follow_up_1",
                         subject="Quick AI audit for Test Pools",
                         body="Hi Tim\n\nI ran a quick AI findability scan on your site this morning and found a couple of things worth a 15-min chat about.\n\n— Sebastian")
    await db.commit()
    return {"db": db, "bdr": bdr, "company": co, "contact": ct,
            "steps": {"cold": s1, "linkedin": s2, "call": s3, "follow_up": s4}}
