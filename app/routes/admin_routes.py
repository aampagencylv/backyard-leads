"""
Platform admin routes — the "GHL-like" console layer above tenants.

Restricted to super_admin. These endpoints intentionally bypass the
tenant auto-filter (they use get_db, not get_tenant_db) because the
admin sees across all tenants.

Endpoints:
  GET    /api/admin/tenants                 list tenants
  POST   /api/admin/tenants                 create a tenant
  PATCH  /api/admin/tenants/{id}            update name/plan/status
  GET    /api/admin/tenants/{id}            tenant detail (counts, last activity)
  POST   /api/admin/tenants/{id}/impersonate  mint an impersonation JWT
  POST   /api/admin/impersonate/end         return to home tenant

  GET    /api/admin/tenants/{id}/domains    list a tenant's custom domains
  POST   /api/admin/tenants/{id}/domains    register a custom domain
  DELETE /api/admin/domains/{domain_id}     remove a custom domain
"""
from __future__ import annotations
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_access_token, require_super_admin
from app.database import get_db
from app.models import Tenant, TenantDomain, User, Company, Contact, Campaign, RuntimeConfig
from app.services.audit_log import record_audit

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ----------------------------------------------------------------------
# Tenant management
# ----------------------------------------------------------------------

class TenantOut(BaseModel):
    id: int
    name: str
    slug: str
    status: str
    plan: str
    created_at: datetime


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=2, max_length=64,
                      description="URL-safe identifier, becomes {slug}.leadprospector.ai")
    plan: str = "starter"


class TenantPatch(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None  # active | suspended
    plan: Optional[str] = None


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")

# Reserved slugs that collide with platform-owned hostnames or routes.
# A tenant can't claim these — would either intercept admin login or be
# rejected by the Caddy ask endpoint.
_RESERVED_SLUGS = {
    "app", "www", "edge", "api", "admin", "auth", "login",
    "mail", "blog", "docs", "help", "support", "billing",
    "status", "static", "assets", "cdn", "dashboard",
}


@router.get("/tenants", response_model=list[TenantOut])
async def list_tenants(
    include_metrics: bool = False,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """List every tenant. When `include_metrics=true`, also bundle
    per-tenant user count + 30-day spend so the admin dashboard's
    card grid renders in one round-trip instead of N+1.
    """
    rows = (await db.execute(select(Tenant).order_by(Tenant.id))).scalars().all()
    out = [TenantOut.model_validate(t, from_attributes=True).model_dump() for t in rows]

    if include_metrics and rows:
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import func as _func
        from app.models import CreditLedger
        tenant_ids = [t.id for t in rows]
        window_start = datetime.now(timezone.utc) - timedelta(days=30)

        user_counts = dict((await db.execute(
            select(User.tenant_id, _func.count())
            .where(User.tenant_id.in_(tenant_ids))
            .group_by(User.tenant_id)
        )).all())

        spend_rows = (await db.execute(
            select(CreditLedger.tenant_id,
                   _func.coalesce(_func.sum(CreditLedger.raw_cost_usd), 0.0))
            .where(CreditLedger.tenant_id.in_(tenant_ids),
                   CreditLedger.created_at >= window_start)
            .group_by(CreditLedger.tenant_id)
        )).all()
        spend_30d = {row[0]: float(row[1]) for row in spend_rows}

        for t in out:
            t["user_count"] = int(user_counts.get(t["id"], 0))
            t["spend_30d_usd"] = float(spend_30d.get(t["id"], 0.0))

    return out


@router.get("/dashboard")
async def admin_dashboard(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """One-shot summary for the platform admin dashboard home page.

    Returns:
      - kpis: total active tenants, total users, today $, last-30d $
      - recent_tenants: 5 most recently created
      - top_spenders: top-5 tenants by 30d raw_cost_usd
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import func as _func
    from app.models import CreditLedger

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now - timedelta(days=30)

    tenants_active = (await db.execute(
        select(_func.count()).select_from(Tenant).where(Tenant.status == "active")
    )).scalar() or 0
    tenants_total = (await db.execute(
        select(_func.count()).select_from(Tenant)
    )).scalar() or 0
    users_total = (await db.execute(
        select(_func.count()).select_from(User).where(User.is_active == True)
    )).scalar() or 0
    spend_today = float((await db.execute(
        select(_func.coalesce(_func.sum(CreditLedger.raw_cost_usd), 0.0))
        .where(CreditLedger.created_at >= today_start)
    )).scalar() or 0)
    spend_30d = float((await db.execute(
        select(_func.coalesce(_func.sum(CreditLedger.raw_cost_usd), 0.0))
        .where(CreditLedger.created_at >= month_start)
    )).scalar() or 0)

    recent_rows = (await db.execute(
        select(Tenant).order_by(Tenant.created_at.desc()).limit(5)
    )).scalars().all()
    recent_tenants = [{
        "id": t.id, "name": t.name, "slug": t.slug, "status": t.status,
        "onboarding_step": t.onboarding_step,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    } for t in recent_rows]

    top_rows = (await db.execute(
        select(CreditLedger.tenant_id,
               _func.coalesce(_func.sum(CreditLedger.raw_cost_usd), 0.0).label("usd"))
        .where(CreditLedger.created_at >= month_start)
        .group_by(CreditLedger.tenant_id)
        .order_by(_func.sum(CreditLedger.raw_cost_usd).desc())
        .limit(5)
    )).all()
    top_tenant_ids = [r[0] for r in top_rows if r[0]]
    top_names: dict[int, str] = {}
    if top_tenant_ids:
        name_rows = (await db.execute(
            select(Tenant.id, Tenant.name).where(Tenant.id.in_(top_tenant_ids))
        )).all()
        top_names = {row.id: row.name for row in name_rows}
    top_spenders = [{
        "tenant_id": r[0],
        "tenant_name": top_names.get(r[0], f"#{r[0]}"),
        "usd": float(r[1]),
    } for r in top_rows]

    return {
        "kpis": {
            "tenants_active": int(tenants_active),
            "tenants_total": int(tenants_total),
            "users_total": int(users_total),
            "spend_today_usd": spend_today,
            "spend_30d_usd": spend_30d,
        },
        "recent_tenants": recent_tenants,
        "top_spenders": top_spenders,
    }


@router.post("/tenants", response_model=TenantOut, status_code=201)
async def create_tenant(
    req: TenantCreate,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
):
    slug = req.slug.lower().strip()
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400,
                            detail="slug must be lowercase letters/digits/hyphens, 4-64 chars")
    if slug in _RESERVED_SLUGS:
        raise HTTPException(status_code=400,
                            detail=f"slug '{slug}' is reserved — collides with a platform hostname")

    existing = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"slug '{slug}' already taken")

    tenant = Tenant(
        name=req.name.strip(),
        slug=slug,
        plan=req.plan,
        status="active",
    )
    db.add(tenant)
    await db.flush()  # need tenant.id before seeding the RuntimeConfig

    # Provision a Twilio sub-account so the tenant has isolated voice/SMS
    # from day one. Best-effort — if TWILIO_MASTER_* isn't configured, or
    # the API returns an error, we still create the tenant and surface
    # the missing creds in the admin UI.
    twilio_sid = None
    twilio_token = None
    try:
        from app.services.twilio_provisioning import create_sub_account
        sub = await create_sub_account(friendly_name=f"LeadProspector · {tenant.name[:48]}")
        if sub:
            twilio_sid, twilio_token = sub
    except Exception:
        # twilio_provisioning never raises by contract, but defense-in-depth.
        pass

    # Provision a Resend sending domain for the tenant. Best-effort.
    # On success we get back the SPF/DKIM/DMARC records — then we try to
    # auto-add them to Cloudflare DNS so the domain self-verifies. If
    # either step fails (creds missing, API timeout), the records still
    # surface in /admin for manual copy.
    resend_domain_id = None
    resend_domain_name = None
    resend_records_json = None
    resend_status = None
    try:
        from app.services.resend_provisioning import create_domain
        import json as _json
        result = await create_domain(subdomain=f"go.{slug}")
        if result:
            resend_domain_id = result["domain_id"]
            resend_domain_name = result["domain_name"]
            resend_records_json = _json.dumps(result["records"])
            resend_status = result["status"]
            # Auto-add to Cloudflare DNS if configured.
            try:
                from app.services.cloudflare_dns import add_resend_records, is_configured as cf_ok
                if cf_ok():
                    added = await add_resend_records(result["records"])
                    if added:
                        # Stamp a marker on the status so the admin UI can
                        # show "DNS auto-added — waiting on Resend verification"
                        # instead of "copy these records to DNS".
                        resend_status = "dns_auto_added"
            except Exception:
                pass
    except Exception:
        pass

    # Seed a RuntimeConfig row so every per-tenant accessor (Twilio creds,
    # brand colors, send window, pipeline stages) finds something on first
    # call instead of upserting a row at the wrong moment in a request.
    # The brand_company_name defaults to the tenant's display name so the
    # first cold email out the door for them already says their name.
    db.add(RuntimeConfig(
        tenant_id=tenant.id,
        brand_company_name=tenant.name[:120],
        twilio_account_sid=twilio_sid,
        twilio_auth_token=twilio_token,
        resend_domain_id=resend_domain_id,
        resend_domain_name=resend_domain_name,
        resend_domain_records_json=resend_records_json,
        resend_domain_status=resend_status,
    ))

    # Auto-register {slug}.leadprospector.ai as a verified tenant domain so
    # Caddy's on-demand TLS ask endpoint accepts the cert request the first
    # time someone hits the URL. Verified=True is safe because the slug
    # subdomain is under DNS we control (wildcard A record on
    # leadprospector.ai) — there's no third-party DNS verification needed.
    platform_host = f"{slug}.leadprospector.ai"
    db.add(TenantDomain(
        tenant_id=tenant.id,
        domain=platform_host,
        is_primary=True,
        is_verified=True,
        verified_at=datetime.now(timezone.utc),
    ))

    # Engagement-engine scaffolding so the new tenant can run the engine
    # the moment they import a contact. Each row is best-effort; if the
    # table is missing on a partial migration, we don't block tenant
    # creation. The tenant_ai_config row is lazy-created elsewhere
    # (engagement_engine_routes), so we don't need to insert it here.
    await _provision_engagement_engine_scaffolding(
        db, tenant_id=tenant.id, tenant_name=tenant.name,
        created_by_user_id=actor.id,
    )

    await db.commit()
    await db.refresh(tenant)
    await record_audit(db, actor=actor, action="tenant_created",
                       target_type="tenant", target_id=tenant.id,
                       metadata={"name": tenant.name, "slug": tenant.slug,
                                 "platform_host": platform_host})
    return TenantOut.model_validate(tenant, from_attributes=True)


@router.patch("/tenants/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    tenant_id: int,
    req: TenantPatch,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
):
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="tenant not found")
    if tenant.id == 1 and req.status == "suspended":
        raise HTTPException(status_code=400, detail="cannot suspend tenant #1 (BMP)")

    changes = {}
    if req.name is not None:
        changes["name"] = req.name
        tenant.name = req.name
    if req.status is not None:
        if req.status not in ("active", "suspended"):
            raise HTTPException(status_code=400, detail="status must be active or suspended")
        changes["status"] = req.status
        tenant.status = req.status
    if req.plan is not None:
        changes["plan"] = req.plan
        tenant.plan = req.plan
    tenant.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(tenant)
    if changes:
        await record_audit(db, actor=actor, action="tenant_updated",
                           target_type="tenant", target_id=tenant.id, metadata=changes)
    return TenantOut.model_validate(tenant, from_attributes=True)


@router.get("/tenants/{tenant_id}")
async def tenant_detail(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="tenant not found")

    # Counts across the major tables (cross-tenant session, explicit WHERE).
    users_count = (await db.execute(
        select(func.count()).select_from(User).where(User.tenant_id == tenant_id)
    )).scalar() or 0
    companies_count = (await db.execute(
        select(func.count()).select_from(Company).where(Company.tenant_id == tenant_id)
    )).scalar() or 0
    contacts_count = (await db.execute(
        select(func.count()).select_from(Contact).where(Contact.tenant_id == tenant_id)
    )).scalar() or 0
    campaigns_count = (await db.execute(
        select(func.count()).select_from(Campaign).where(Campaign.tenant_id == tenant_id)
    )).scalar() or 0

    domains = (await db.execute(
        select(TenantDomain).where(TenantDomain.tenant_id == tenant_id)
    )).scalars().all()

    # Pull this tenant's RuntimeConfig directly (cross-tenant session, so
    # we filter manually). Used by the admin UI to surface provisioning
    # status: does the tenant have a Twilio sub-account? Resend domain?
    # Are the DNS records ready to copy to the registrar?
    import json as _json
    rc = (await db.execute(
        select(RuntimeConfig).where(RuntimeConfig.tenant_id == tenant_id)
    )).scalar_one_or_none()
    provisioning = {
        "twilio_subaccount_sid": (rc.twilio_account_sid[:8] + "..." if rc and rc.twilio_account_sid else None),
        "resend_domain_id": rc.resend_domain_id if rc else None,
        "resend_domain_name": rc.resend_domain_name if rc else None,
        "resend_domain_status": rc.resend_domain_status if rc else None,
        "resend_records": (_json.loads(rc.resend_domain_records_json) if rc and rc.resend_domain_records_json else []),
    }

    return {
        "tenant": TenantOut.model_validate(tenant, from_attributes=True).model_dump(),
        "counts": {
            "users": users_count,
            "companies": companies_count,
            "contacts": contacts_count,
            "campaigns": campaigns_count,
        },
        "domains": [
            {"id": d.id, "domain": d.domain, "is_primary": d.is_primary}
            for d in domains
        ],
        "provisioning": provisioning,
    }


@router.get("/costs")
async def platform_costs(
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """Platform-wide cost summary from credit_ledger.raw_cost_usd.

    Returns totals for three windows (today, 7-day, N-day where N is the
    `days` param, default 30), each broken down by vendor and by tenant.

    raw_cost_usd is what the platform actually pays the vendor (Anthropic,
    Twilio, Resend, Netrows, etc.) — the "true cost of goods" view. Different
    from credits_debited which is what we'd bill customers.
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import func as _func
    from app.models import CreditLedger

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seven_d_start = now - timedelta(days=7)
    window_start = now - timedelta(days=max(1, min(days, 365)))

    async def aggregate(since):
        # Returns three rollups for the window: total, by-vendor, by-tenant.
        total_row = (await db.execute(
            select(
                _func.coalesce(_func.sum(CreditLedger.raw_cost_usd), 0.0).label("usd"),
                _func.count().label("events"),
            ).where(CreditLedger.created_at >= since)
        )).one()

        vendor_rows = (await db.execute(
            select(
                CreditLedger.vendor,
                _func.coalesce(_func.sum(CreditLedger.raw_cost_usd), 0.0).label("usd"),
                _func.count().label("events"),
            )
            .where(CreditLedger.created_at >= since)
            .group_by(CreditLedger.vendor)
            .order_by(_func.sum(CreditLedger.raw_cost_usd).desc())
        )).all()

        tenant_rows = (await db.execute(
            select(
                CreditLedger.tenant_id,
                _func.coalesce(_func.sum(CreditLedger.raw_cost_usd), 0.0).label("usd"),
                _func.count().label("events"),
            )
            .where(CreditLedger.created_at >= since)
            .group_by(CreditLedger.tenant_id)
            .order_by(_func.sum(CreditLedger.raw_cost_usd).desc())
        )).all()

        return {
            "total_usd": float(total_row.usd),
            "events": int(total_row.events),
            "by_vendor": [
                {"vendor": (r.vendor or "internal"), "usd": float(r.usd), "events": int(r.events)}
                for r in vendor_rows
            ],
            "by_tenant": [
                {"tenant_id": r.tenant_id, "usd": float(r.usd), "events": int(r.events)}
                for r in tenant_rows
            ],
        }

    today = await aggregate(today_start)
    week  = await aggregate(seven_d_start)
    win   = await aggregate(window_start)

    # Hydrate tenant names so the UI doesn't have to do an N+1.
    tenant_ids = list({t["tenant_id"] for t in win["by_tenant"] if t["tenant_id"]})
    tenant_names: dict[int, str] = {}
    if tenant_ids:
        rows = (await db.execute(
            select(Tenant.id, Tenant.name).where(Tenant.id.in_(tenant_ids))
        )).all()
        tenant_names = {row.id: row.name for row in rows}
    for window in (today, week, win):
        for t in window["by_tenant"]:
            t["tenant_name"] = tenant_names.get(t["tenant_id"], f"#{t['tenant_id']}")

    return {
        "today": today,
        "seven_day": week,
        "window_days": days,
        "window": win,
        "generated_at": now.isoformat(),
    }


@router.get("/tenants/{tenant_id}/costs")
async def tenant_costs(
    tenant_id: int,
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """Cost breakdown for a single tenant — used by the tenant detail
    panel. Same shape as /api/admin/costs but scoped to one tenant_id."""
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import func as _func
    from app.models import CreditLedger

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=max(1, min(days, 365)))

    total = (await db.execute(
        select(
            _func.coalesce(_func.sum(CreditLedger.raw_cost_usd), 0.0).label("usd"),
            _func.count().label("events"),
        ).where(
            CreditLedger.tenant_id == tenant_id,
            CreditLedger.created_at >= window_start,
        )
    )).one()

    by_vendor = (await db.execute(
        select(
            CreditLedger.vendor,
            _func.coalesce(_func.sum(CreditLedger.raw_cost_usd), 0.0).label("usd"),
            _func.count().label("events"),
        )
        .where(
            CreditLedger.tenant_id == tenant_id,
            CreditLedger.created_at >= window_start,
        )
        .group_by(CreditLedger.vendor)
        .order_by(_func.sum(CreditLedger.raw_cost_usd).desc())
    )).all()

    by_action = (await db.execute(
        select(
            CreditLedger.action_type,
            _func.coalesce(_func.sum(CreditLedger.raw_cost_usd), 0.0).label("usd"),
            _func.count().label("events"),
        )
        .where(
            CreditLedger.tenant_id == tenant_id,
            CreditLedger.created_at >= window_start,
        )
        .group_by(CreditLedger.action_type)
        .order_by(_func.sum(CreditLedger.raw_cost_usd).desc())
    )).all()

    return {
        "tenant_id": tenant_id,
        "window_days": days,
        "total_usd": float(total.usd),
        "events": int(total.events),
        "by_vendor": [
            {"vendor": (r.vendor or "internal"), "usd": float(r.usd), "events": int(r.events)}
            for r in by_vendor
        ],
        "by_action": [
            {"action_type": r.action_type, "usd": float(r.usd), "events": int(r.events)}
            for r in by_action
        ],
    }


@router.get("/feedback")
async def admin_feedback(
    resolved: Optional[bool] = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """Cross-tenant team-feedback feed for the platform admin console.

    Replaces the per-tenant feedback management UI in BMP Settings —
    Steve sees every tenant's bug reports + feature requests + general
    feedback in one place. Tenant admins no longer see this view; only
    super_admin.

    Submission still goes through POST /api/feedback in the tenant
    app (any logged-in user can submit). This endpoint just lists.
    """
    from app.models import Feedback
    q = select(Feedback, User).join(User, Feedback.user_id == User.id)
    if resolved is not None:
        q = q.where(Feedback.resolved == resolved)
    q = q.order_by(Feedback.created_at.desc()).limit(min(limit, 500))
    rows = (await db.execute(q)).all()

    tenant_ids = list({u.tenant_id for _, u in rows})
    tenant_names: dict[int, str] = {}
    if tenant_ids:
        t_rows = (await db.execute(
            select(Tenant.id, Tenant.name).where(Tenant.id.in_(tenant_ids))
        )).all()
        tenant_names = {r.id: r.name for r in t_rows}

    return {
        "items": [{
            "id": f.id,
            "category": f.category,
            "message": f.message,
            "page": f.page,
            "resolved": f.resolved,
            "admin_notes": f.admin_notes,
            "user_id": u.id,
            "user_email": u.email,
            "user_name": u.full_name,
            "tenant_id": u.tenant_id,
            "tenant_name": tenant_names.get(u.tenant_id, f"#{u.tenant_id}"),
            "created_at": f.created_at.isoformat() if f.created_at else None,
        } for f, u in rows],
        "count": len(rows),
    }


@router.patch("/feedback/{feedback_id}")
async def admin_update_feedback(
    feedback_id: int,
    resolved: Optional[bool] = None,
    admin_notes: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
):
    """Resolve or annotate a feedback item from the platform admin console.
    Cross-tenant — finds the feedback regardless of which tenant the
    submitter belonged to."""
    from app.models import Feedback
    fb = (await db.execute(select(Feedback).where(Feedback.id == feedback_id))).scalar_one_or_none()
    if not fb:
        raise HTTPException(status_code=404, detail="feedback not found")
    if resolved is not None:
        fb.resolved = resolved
    if admin_notes is not None:
        fb.admin_notes = admin_notes.strip()[:500]
    await db.commit()
    return {"id": fb.id, "resolved": fb.resolved, "admin_notes": fb.admin_notes}


@router.get("/platform-keys")
async def platform_keys(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """Platform-wide credentials. These are the keys used by every tenant
    on every action — not per-tenant. Steve's single source of truth.

    Two storage tiers, displayed together:
      - env: read from VPS .env via os.environ (rotation needs SSH)
      - db:  stored on the singleton "platform" RuntimeConfig row
             (tenant 1 / BMP carries the canonical platform values today)

    Values are MASKED (first 8 + last 4) — full secrets never leave the
    server through this endpoint.
    """
    import os
    from app.runtime_config import mask_key

    def env_key(name: str) -> dict:
        v = (os.environ.get(name) or "").strip()
        return {"name": name, "source": "env", "set": bool(v), "masked": mask_key(v) if v else None}

    def db_key(label: str, val: str | None) -> dict:
        v = (val or "").strip()
        return {"name": label, "source": "db", "set": bool(v), "masked": mask_key(v) if v else None}

    # The platform RuntimeConfig is tenant 1's row by convention (BMP). This is
    # safe to fetch unscoped because we use get_db (no auto-filter).
    rc = (await db.execute(
        select(RuntimeConfig).where(RuntimeConfig.tenant_id == 1)
    )).scalar_one_or_none()

    sections = []

    # === AI / generation ===
    sections.append({
        "service": "Anthropic",
        "purpose": "Claude API — email generation, reply classification, AI chat",
        "keys": [env_key("ANTHROPIC_API_KEY")],
    })

    # === Email infrastructure ===
    sections.append({
        "service": "Resend (Platform)",
        "purpose": "System emails — invites, password resets, platform notifications",
        "keys": [env_key("PLATFORM_RESEND_API_KEY"), env_key("PLATFORM_SENDING_DOMAIN")],
    })
    sections.append({
        "service": "Resend (Tenant-prospect sending)",
        "purpose": "Where new tenants' go.{slug}.leadprospector.ai domains are provisioned",
        "keys": [
            env_key("RESEND_API_KEY"),
            db_key("Webhook secret (DB override)", rc.resend_webhook_secret if rc else None),
            env_key("RESEND_WEBHOOK_SECRET"),
        ],
    })

    # === Telephony ===
    sections.append({
        "service": "Twilio (Master)",
        "purpose": "Parent account — creates per-tenant sub-accounts on tenant create",
        "keys": [
            env_key("TWILIO_MASTER_ACCOUNT_SID"),
            env_key("TWILIO_MASTER_AUTH_TOKEN"),
        ],
    })
    sections.append({
        "service": "Deepgram",
        "purpose": "Call transcription + speaker diarization",
        "keys": [
            env_key("DEEPGRAM_API_KEY"),
            db_key("DB override", rc.deepgram_api_key if rc else None),
        ],
    })
    sections.append({
        "service": "Blooio",
        "purpose": "iMessage automation (platform-brokered today; per-tenant once we onboard tenants with their own Blooio)",
        "keys": [
            db_key("API key", rc.blooio_api_key if rc else None),
            db_key("Signing secret", rc.blooio_signing_secret if rc else None),
        ],
    })

    # === Data / enrichment ===
    sections.append({
        "service": "Netrows",
        "purpose": "Proprietary enrichment — platform-only, never BYO",
        "keys": [
            env_key("NETROWS_API_KEY"),
            db_key("DB override", rc.netrows_api_key if rc else None),
        ],
    })
    sections.append({
        "service": "Hunter",
        "purpose": "Email enrichment fallback",
        "keys": [env_key("HUNTER_API_KEY")],
    })
    sections.append({
        "service": "Google Maps",
        "purpose": "/find-leads Places API + nearby search",
        "keys": [
            env_key("GOOGLE_MAPS_API_KEY"),
            db_key("DB override", rc.google_maps_api_key if rc else None),
        ],
    })
    sections.append({
        "service": "DataForSEO",
        "purpose": "Audit-report SEO scores",
        "keys": [env_key("DATAFORSEO_LOGIN"), env_key("DATAFORSEO_PASSWORD")],
    })

    # === Infra / observability ===
    sections.append({
        "service": "Cloudflare",
        "purpose": "leadprospector.ai DNS — auto-adds tenant Resend records",
        "keys": [env_key("CLOUDFLARE_API_TOKEN"), env_key("CLOUDFLARE_ZONE_ID")],
    })
    sections.append({
        "service": "Sentry",
        "purpose": "Error tracking + API access for triage",
        "keys": [env_key("SENTRY_DSN"), env_key("SENTRY_AUTH_TOKEN")],
    })

    # === Booking integrations ===
    sections.append({
        "service": "iClosed",
        "purpose": "Booking-page integration (audit-report scheduler)",
        "keys": [env_key("ICLOSED_API_KEY"), env_key("ICLOSED_WEBHOOK_SECRET")],
    })

    return {"sections": sections}


@router.get("/users")
async def list_all_users(
    search: Optional[str] = None,
    tenant_id: Optional[int] = None,
    role: Optional[str] = None,
    active_only: bool = False,
    limit: int = 200,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """Cross-tenant user listing for the platform admin console.

    Filters:
      search       — substring match on email + first/last name
      tenant_id    — narrow to one tenant
      role         — exact-match (super_admin / admin / senior_rep / sales_rep / read_only)
      active_only  — only is_active=True

    Returns up to `limit` users joined with their tenant name. Sorted
    by tenant_id then email for a predictable readout.
    """
    q = select(User)
    if tenant_id is not None:
        q = q.where(User.tenant_id == tenant_id)
    if role:
        q = q.where(User.role == role)
    if active_only:
        q = q.where(User.is_active == True)
    if search:
        s = f"%{search.strip().lower()}%"
        from sqlalchemy import or_, func as _func
        q = q.where(or_(
            _func.lower(User.email).like(s),
            _func.lower(User.first_name).like(s),
            _func.lower(User.last_name).like(s),
        ))
    q = q.order_by(User.tenant_id, User.email).limit(min(limit, 500)).offset(offset)
    rows = (await db.execute(q)).scalars().all()

    tenant_ids = list({u.tenant_id for u in rows})
    tenant_names: dict[int, str] = {}
    if tenant_ids:
        t_rows = (await db.execute(
            select(Tenant.id, Tenant.name).where(Tenant.id.in_(tenant_ids))
        )).all()
        tenant_names = {r.id: r.name for r in t_rows}

    return {
        "items": [{
            "id": u.id,
            "tenant_id": u.tenant_id,
            "tenant_name": tenant_names.get(u.tenant_id, f"#{u.tenant_id}"),
            "email": u.email,
            "full_name": u.full_name,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "role": u.role,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat() if getattr(u, "last_login_at", None) else None,
        } for u in rows],
        "count": len(rows),
        "offset": offset,
        "limit": limit,
    }


class AdminUserPatch(BaseModel):
    role: Optional[str] = None        # super_admin / admin / senior_rep / sales_rep / read_only
    is_active: Optional[bool] = None  # toggle access without deleting


@router.patch("/users/{user_id}")
async def admin_update_user(
    user_id: int,
    req: AdminUserPatch,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
):
    """Cross-tenant user mutation. Super_admin only.

    Used by /admin's Users panel to:
      - change a user's role (promote sales_rep → admin, etc.)
      - toggle is_active (suspend without deleting)

    Self-protection: a super_admin can't demote themselves or
    deactivate themselves from this endpoint — easy way to lock
    yourself out of the platform.
    """
    u = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not u:
        raise HTTPException(status_code=404, detail="user not found")

    if u.id == actor.id and req.role is not None and req.role != "super_admin":
        raise HTTPException(status_code=400, detail="Can't demote yourself")
    if u.id == actor.id and req.is_active is False:
        raise HTTPException(status_code=400, detail="Can't deactivate yourself")

    changes = {}
    if req.role is not None:
        valid = {"super_admin", "admin", "senior_rep", "sales_rep", "read_only"}
        if req.role not in valid:
            raise HTTPException(status_code=400, detail=f"role must be one of {sorted(valid)}")
        changes["role"] = {"from": u.role, "to": req.role}
        u.role = req.role
    if req.is_active is not None:
        changes["is_active"] = {"from": u.is_active, "to": req.is_active}
        u.is_active = req.is_active

    await db.commit()
    await db.refresh(u)
    if changes:
        await record_audit(db, actor=actor, action="admin_user_updated",
                           target_type="user", target_id=u.id,
                           metadata={"tenant_id": u.tenant_id, "changes": changes})
    return {"id": u.id, "email": u.email, "role": u.role, "is_active": u.is_active, "tenant_id": u.tenant_id}


@router.get("/tenants/{tenant_id}/keys")
async def tenant_api_keys(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """Per-tenant API Keys vault. Super_admin only — never exposed to
    the tenant's own users.

    Returns the same `{set, masked}` shape the legacy Settings UI used
    for the platform-tier credentials, but scoped to one tenant. Used
    by /admin's tenant detail page to show Steve every credential we
    have on file for a given tenant in one glance.

    Auth tokens / signing secrets are MASKED — the first 8 chars + the
    last 4. Never the full value. Steve can still rotate via the
    tenant's own /api/runtime-config PATCH (with a super_admin token)
    if he needs to.
    """
    rc = (await db.execute(
        select(RuntimeConfig).where(RuntimeConfig.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if not rc:
        raise HTTPException(status_code=404, detail="tenant has no RuntimeConfig")

    from app.runtime_config import mask_key

    def t(field):
        v = (field or "").strip()
        return {"set": bool(v), "masked": mask_key(v) if v else None}

    return {
        "tenant_id": tenant_id,
        "twilio": {
            "account_sid":    t(rc.twilio_account_sid),
            "auth_token":     t(rc.twilio_auth_token),
            "api_key_sid":    t(rc.twilio_api_key_sid),
            "api_key_secret": t(rc.twilio_api_key_secret),
            "twiml_app_sid":  t(rc.twilio_twiml_app_sid),
        },
        "netrows":     t(rc.netrows_api_key),
        "deepgram":    t(rc.deepgram_api_key),
        "blooio": {
            "api_key":        t(rc.blooio_api_key),
            "signing_secret": t(rc.blooio_signing_secret),
        },
        "resend": {
            "webhook_secret": t(rc.resend_webhook_secret),
            "domain_id":      rc.resend_domain_id,
            "domain_name":    rc.resend_domain_name,
            "domain_status":  rc.resend_domain_status,
        },
        "google_maps": t(rc.google_maps_api_key),
        "apollo":      t(rc.apollo_api_key),
    }


@router.post("/tenants/{tenant_id}/refresh-email-status")
async def refresh_email_status(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """Re-fetch the Resend domain status for this tenant — useful after
    the platform admin has added the DNS records and wants to confirm
    Resend has verified the domain."""
    rc = (await db.execute(
        select(RuntimeConfig).where(RuntimeConfig.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if not rc or not rc.resend_domain_id:
        raise HTTPException(status_code=404, detail="tenant has no Resend domain provisioned")
    from app.services.resend_provisioning import get_domain_status
    status_data = await get_domain_status(rc.resend_domain_id)
    if not status_data:
        raise HTTPException(status_code=502, detail="Could not fetch status from Resend")
    new_status = status_data.get("status") or rc.resend_domain_status
    rc.resend_domain_status = new_status
    await db.commit()
    return {"status": new_status, "domain_name": rc.resend_domain_name}


# ----------------------------------------------------------------------
# Email sending-domain provisioning (Resend)
# ----------------------------------------------------------------------

class EmailDomainProvisionIn(BaseModel):
    # Full sending domain. Omit to default to go.{slug}.leadprospector.ai
    # (platform-hosted, auto-DNS). For a customer's own brand, pass e.g.
    # "go.aamp.agency" — the customer then adds the returned records to
    # their own DNS.
    domain_name: Optional[str] = None


def _format_dns_records(records: list) -> list[dict]:
    """Normalize Resend's record objects into a copy-paste-friendly shape
    for the admin UI: type / name / value / priority / status."""
    out = []
    for rec in records or []:
        out.append({
            "type": (rec.get("type") or rec.get("record") or "").upper(),
            "name": rec.get("name") or "",
            "value": rec.get("value") or rec.get("content") or "",
            "priority": rec.get("priority"),
            "ttl": rec.get("ttl") or "Auto",
            "status": rec.get("status") or "",
        })
    return out


@router.post("/tenants/{tenant_id}/email-domain")
async def provision_email_domain(
    tenant_id: int,
    req: EmailDomainProvisionIn,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
):
    """Register a sending domain in Resend for this tenant and capture the
    DNS records they need to add. Works for a customer's own domain
    (go.aamp.agency) or a platform-hosted subdomain. For platform domains
    we also auto-add the records to Cloudflare; for customer domains we
    return the records for the customer to add to their own DNS."""
    import json as _json
    from app.services.resend_provisioning import (
        register_domain, is_platform_domain, is_configured,
    )
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="tenant not found")
    if not is_configured():
        raise HTTPException(status_code=400, detail="Resend API key not configured on the platform")

    domain_name = (req.domain_name or f"go.{tenant.slug}.leadprospector.ai").strip().lower().rstrip(".")
    result = await register_domain(domain_name)
    if not result:
        raise HTTPException(
            status_code=502,
            detail=f"Resend could not register '{domain_name}'. It may already exist, or the domain is invalid.",
        )

    rc = (await db.execute(
        select(RuntimeConfig).where(RuntimeConfig.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if not rc:
        rc = RuntimeConfig(tenant_id=tenant_id)
        db.add(rc)
    rc.resend_domain_id = result["domain_id"]
    rc.resend_domain_name = result["domain_name"]
    rc.resend_domain_records_json = _json.dumps(result["records"])
    rc.resend_domain_status = result["status"]

    # Platform-hosted domains: we control the DNS, so auto-add to Cloudflare.
    auto_dns = False
    if is_platform_domain(result["domain_name"]):
        try:
            from app.services.cloudflare_dns import add_resend_records, is_configured as cf_ok
            if cf_ok():
                if await add_resend_records(result["records"]):
                    rc.resend_domain_status = "dns_auto_added"
                    auto_dns = True
        except Exception:
            pass

    await db.commit()
    await record_audit(db, actor=actor, action="email_domain_provisioned",
                       target_type="tenant", target_id=tenant_id,
                       metadata={"domain": result["domain_name"], "auto_dns": auto_dns})
    return {
        "domain_name": result["domain_name"],
        "domain_id": result["domain_id"],
        "status": rc.resend_domain_status,
        "is_platform_domain": is_platform_domain(result["domain_name"]),
        "auto_dns_added": auto_dns,
        "records": _format_dns_records(result["records"]),
    }


@router.get("/tenants/{tenant_id}/email-domain")
async def get_email_domain(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """Current sending-domain config + DNS records for the admin UI."""
    import json as _json
    from app.services.resend_provisioning import is_platform_domain
    rc = (await db.execute(
        select(RuntimeConfig).where(RuntimeConfig.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if not rc or not rc.resend_domain_id:
        return {"configured": False}
    records = _json.loads(rc.resend_domain_records_json) if rc.resend_domain_records_json else []
    return {
        "configured": True,
        "domain_name": rc.resend_domain_name,
        "domain_id": rc.resend_domain_id,
        "status": rc.resend_domain_status,
        "is_platform_domain": is_platform_domain(rc.resend_domain_name or ""),
        "records": _format_dns_records(records),
    }


@router.post("/tenants/{tenant_id}/email-domain/verify")
async def verify_email_domain(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """Ask Resend to re-check DNS (call after records are added), then
    refresh the stored status + per-record verification state."""
    import json as _json
    from app.services.resend_provisioning import trigger_verify, get_domain_status
    rc = (await db.execute(
        select(RuntimeConfig).where(RuntimeConfig.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if not rc or not rc.resend_domain_id:
        raise HTTPException(status_code=404, detail="tenant has no Resend domain provisioned")

    await trigger_verify(rc.resend_domain_id)
    # Re-fetch the authoritative status + records after the verify poke.
    status_data = await get_domain_status(rc.resend_domain_id)
    if not status_data:
        raise HTTPException(status_code=502, detail="Could not fetch status from Resend")
    new_status = status_data.get("status") or rc.resend_domain_status
    new_records = status_data.get("records") or []
    rc.resend_domain_status = new_status
    if new_records:
        rc.resend_domain_records_json = _json.dumps(new_records)
    await db.commit()
    return {
        "domain_name": rc.resend_domain_name,
        "status": new_status,
        "records": _format_dns_records(new_records or (_json.loads(rc.resend_domain_records_json) if rc.resend_domain_records_json else [])),
    }


# ----------------------------------------------------------------------
# Per-tenant AI messaging direction (used by the New Company wizard)
# ----------------------------------------------------------------------

class MessagingDirectionIn(BaseModel):
    text: str = ""


@router.post("/tenants/{tenant_id}/messaging-direction")
async def set_tenant_messaging_direction(
    tenant_id: int,
    req: MessagingDirectionIn,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
):
    """Set a specific tenant's AI messaging direction — the strategic angle
    prepended to every AI generation (cold email, follow-ups, iMessage).
    BMP's is 'AI findability / local SEO'; AAMP's might be 'web dev +
    Google Ads for tour operators'. This is what 'vertical' really maps to."""
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="tenant not found")
    rc = (await db.execute(
        select(RuntimeConfig).where(RuntimeConfig.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if not rc:
        rc = RuntimeConfig(tenant_id=tenant_id)
        db.add(rc)
    rc.messaging_direction = (req.text or "").strip() or None
    await db.commit()
    await record_audit(db, actor=actor, action="messaging_direction_set",
                       target_type="tenant", target_id=tenant_id,
                       metadata={"chars": len(req.text or "")})
    return {"tenant_id": tenant_id, "messaging_direction": rc.messaging_direction or ""}


# ----------------------------------------------------------------------
# Impersonation
# ----------------------------------------------------------------------

class ImpersonateOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    acting_as_tenant_id: int
    redirect_url: str  # tenant app URL the console should navigate to


@router.post("/tenants/{tenant_id}/impersonate", response_model=ImpersonateOut)
async def impersonate_tenant(
    tenant_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
):
    """Mint a JWT carrying acting_as_tenant_id. The tenancy resolver
    checks this claim first, so all subsequent requests resolve to the
    impersonated tenant. The UI should display a persistent red banner
    while this token is in use."""
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="tenant not found")

    token = create_access_token({
        "sub": str(actor.id),
        "tenant_id": actor.tenant_id,                # the admin's home tenant
        "acting_as_tenant_id": tenant.id,            # who they're impersonating
    })

    # Resolve the tenant's app URL so the console can navigate there (the
    # CRM SPA is served on tenant hosts, NOT on app.leadprospector.ai where
    # `/` is the admin console). Mirror universal-login: primary domain →
    # any verified domain → app.leadprospector.ai fallback. The acting token
    # rides across the subdomain hop as ?_lp_token=, which index.html reads
    # into localStorage on load.
    primary = (await db.execute(
        select(TenantDomain).where(
            TenantDomain.tenant_id == tenant.id,
            TenantDomain.is_primary == True,
        ).limit(1)
    )).scalar_one_or_none()
    if primary is None:
        primary = (await db.execute(
            select(TenantDomain).where(
                TenantDomain.tenant_id == tenant.id,
                TenantDomain.is_verified == True,
            ).limit(1)
        )).scalar_one_or_none()
    host = primary.domain if primary else "app.leadprospector.ai"
    redirect_url = f"https://{host}/"

    await record_audit(db, actor=actor, action="tenant_impersonate_start",
                       target_type="tenant", target_id=tenant.id,
                       metadata={"tenant_name": tenant.name,
                                 "ip": request.client.host if request.client else None})
    return ImpersonateOut(access_token=token, acting_as_tenant_id=tenant.id,
                          redirect_url=redirect_url)


class EndImpersonateOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/impersonate/end", response_model=EndImpersonateOut)
async def end_impersonation(
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
):
    """Return a clean token (no acting_as claim) so the UI can swap back."""
    token = create_access_token({
        "sub": str(actor.id),
        "tenant_id": actor.tenant_id,
    })
    await record_audit(db, actor=actor, action="tenant_impersonate_end",
                       target_type="tenant", target_id=actor.tenant_id, metadata={})
    return EndImpersonateOut(access_token=token)


# ----------------------------------------------------------------------
# First-user provisioning inside a tenant
# ----------------------------------------------------------------------

class TenantUserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    role: str = Field(default="super_admin",
                      description="super_admin (default for first user) | admin | sales_rep | senior_rep | read_only")
    temp_password: str = Field(min_length=8, max_length=128,
                               description="One-time password emailed to the user. They reset on first login.")


class TenantUserOut(BaseModel):
    id: int
    tenant_id: int
    email: str
    role: str


@router.post("/tenants/{tenant_id}/users", response_model=TenantUserOut, status_code=201)
async def create_tenant_user(
    tenant_id: int,
    req: TenantUserCreate,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
):
    """Provision a user inside a specific tenant.

    Use this for onboarding the first super_admin of a new tenant. The
    caller is a platform super_admin (always tenant #1 today). The new
    user belongs to the target tenant — every subsequent action they
    take is scoped there.
    """
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="tenant not found")

    valid_roles = {"super_admin", "admin", "senior_rep", "sales_rep", "read_only"}
    if req.role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"role must be one of {sorted(valid_roles)}")

    # Email uniqueness is checked within the target tenant only — two
    # tenants can each have their own steve@example.com.
    existing = (await db.execute(
        select(User).where(User.email == req.email.lower(), User.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="email already exists in this tenant")

    from app.auth import hash_password
    user = User(
        tenant_id=tenant_id,
        email=req.email.lower().strip(),
        first_name=req.first_name.strip(),
        last_name=req.last_name.strip(),
        role=req.role,
        hashed_password=hash_password(req.temp_password),
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    await record_audit(db, actor=actor, action="tenant_user_provisioned",
                       target_type="user", target_id=user.id,
                       metadata={"tenant_id": tenant_id, "email": user.email, "role": user.role})

    # Send the invite email through the LeadProspector platform Resend
    # account. Best-effort: if PLATFORM_RESEND_API_KEY isn't set, the
    # mailer logs and returns None — the API response still includes the
    # temp password so the operator can deliver it manually.
    try:
        from app.services.platform_mailer import send_platform_email
        # Find each tenant's slug subdomain to use as the login URL when set.
        primary = (await db.execute(
            select(TenantDomain).where(
                TenantDomain.tenant_id == tenant_id,
                TenantDomain.is_primary == True,
            )
        )).scalar_one_or_none()
        login_url = f"https://{primary.domain}" if primary else "https://app.leadprospector.ai"
        await send_platform_email(
            to=user.email,
            template="user_invite",
            vars={
                "first_name": user.first_name or user.email.split("@")[0],
                "email": user.email,
                "tenant_name": tenant.name,
                "actor_name": (actor.first_name or "Your platform admin"),
                "login_url": login_url,
                "temp_password": req.temp_password,
            },
        )
    except Exception:
        # Mailer is best-effort; never block user provisioning on email delivery.
        pass

    return TenantUserOut(id=user.id, tenant_id=tenant_id, email=user.email, role=user.role)


# ----------------------------------------------------------------------
# Custom-domain registration
# ----------------------------------------------------------------------

class DomainOut(BaseModel):
    id: int
    tenant_id: int
    domain: str
    is_primary: bool
    is_verified: bool = False
    verified_at: Optional[datetime] = None
    created_at: datetime


class DomainCreate(BaseModel):
    domain: str = Field(min_length=4, max_length=255)
    is_primary: bool = False


_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


@router.get("/tenants/{tenant_id}/domains", response_model=list[DomainOut])
async def list_domains(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    rows = (await db.execute(
        select(TenantDomain).where(TenantDomain.tenant_id == tenant_id).order_by(TenantDomain.id)
    )).scalars().all()
    return [DomainOut.model_validate(d, from_attributes=True) for d in rows]


@router.post("/tenants/{tenant_id}/domains", response_model=DomainOut, status_code=201)
async def add_domain(
    tenant_id: int,
    req: DomainCreate,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
):
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="tenant not found")

    domain = req.domain.lower().strip().rstrip(".")
    if not _DOMAIN_RE.match(domain):
        raise HTTPException(status_code=400, detail="invalid domain format")

    existing = (await db.execute(
        select(TenantDomain).where(TenantDomain.domain == domain)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409,
                            detail=f"domain '{domain}' already registered to tenant {existing.tenant_id}")

    if req.is_primary:
        # Demote any existing primary on this tenant
        existing_primaries = (await db.execute(
            select(TenantDomain).where(
                TenantDomain.tenant_id == tenant_id,
                TenantDomain.is_primary == True,
            )
        )).scalars().all()
        for d in existing_primaries:
            d.is_primary = False

    td = TenantDomain(tenant_id=tenant_id, domain=domain, is_primary=req.is_primary)
    db.add(td)
    await db.commit()
    await db.refresh(td)
    await record_audit(db, actor=actor, action="tenant_domain_added",
                       target_type="tenant", target_id=tenant_id,
                       metadata={"domain": domain, "is_primary": req.is_primary})
    return DomainOut.model_validate(td, from_attributes=True)


@router.delete("/domains/{domain_id}", status_code=204)
async def remove_domain(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
):
    td = (await db.execute(select(TenantDomain).where(TenantDomain.id == domain_id))).scalar_one_or_none()
    if not td:
        raise HTTPException(status_code=404, detail="domain not found")
    if td.tenant_id == 1 and td.domain == "prospector.backyardmarketingpros.com":
        raise HTTPException(status_code=400, detail="cannot remove BMP's primary domain")
    payload = {"tenant_id": td.tenant_id, "domain": td.domain}
    await db.delete(td)
    await db.commit()
    await record_audit(db, actor=actor, action="tenant_domain_removed",
                       target_type="tenant", target_id=payload["tenant_id"],
                       metadata=payload)


# ----------------------------------------------------------------------
# DNS verification for custom domains
# ----------------------------------------------------------------------
#
# A tenant's domain is "verified" when its DNS is pointed at our edge.
# Today we check two signals:
#
#   1. CNAME or A record resolves to our expected target. For now we
#      require CNAME → edge.leadprospector.ai OR an A record matching
#      our VPS IP (env: PLATFORM_EDGE_IP).
#   2. Optional ownership TXT _acmeagency-verify.<domain> = <expected>
#      where <expected> is derived from the tenant id + a salt. Lets
#      large customers prove control without DNS pointing yet.
#
# Caddy on-demand TLS will refuse to issue certs for unverified domains
# (the /caddy/ask endpoint below returns 403 unless the domain is in
# tenant_domains AND its is_verified flag is true).

EDGE_HOSTNAME = "edge.leadprospector.ai"


class DomainVerifyOut(BaseModel):
    domain: str
    cname_target: Optional[str] = None
    a_records: list[str] = []
    txt_records: list[str] = []
    verified: bool
    is_verified_db: bool
    reason: str


def _expected_a_records() -> set[str]:
    import os
    raw = os.environ.get("PLATFORM_EDGE_IP", "72.62.168.160")
    return {ip.strip() for ip in raw.split(",") if ip.strip()}


@router.post("/domains/{domain_id}/verify", response_model=DomainVerifyOut)
async def verify_domain(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
):
    """Live DNS check; flips tenant_domains.is_verified on success.

    Caddy will only issue a cert for a domain whose row has
    is_verified=TRUE (see /caddy/ask). Once verified, demoting the
    DNS later doesn't auto-revoke — an operator must DELETE the
    domain row to disable the cert.
    """
    import dns.resolver
    import dns.exception
    from datetime import datetime as _dt, timezone as _tz

    td = (await db.execute(select(TenantDomain).where(TenantDomain.id == domain_id))).scalar_one_or_none()
    if not td:
        raise HTTPException(status_code=404, detail="domain not found")

    domain = td.domain
    cname_target: Optional[str] = None
    a_records: list[str] = []
    txt_records: list[str] = []
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 5

    try:
        ans = resolver.resolve(domain, "CNAME")
        cname_target = str(ans[0].target).rstrip(".").lower()
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.exception.DNSException):
        pass

    try:
        ans = resolver.resolve(domain, "A")
        a_records = [r.address for r in ans]
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.exception.DNSException):
        pass

    try:
        ans = resolver.resolve(f"_leadprospector.{domain}", "TXT")
        txt_records = [b"".join(r.strings).decode("utf-8", errors="replace") for r in ans]
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.exception.DNSException):
        pass

    cname_ok = cname_target == EDGE_HOSTNAME
    a_ok = bool(a_records) and bool(_expected_a_records() & set(a_records))

    verified = cname_ok or a_ok
    if verified:
        reason = "CNAME ok" if cname_ok else "A record ok"
        if not td.is_verified:
            td.is_verified = True
            td.verified_at = _dt.now(_tz.utc)
            await db.commit()
            await record_audit(db, actor=actor, action="tenant_domain_verified",
                               target_type="tenant", target_id=td.tenant_id,
                               metadata={"domain": domain, "reason": reason})
    elif cname_target:
        reason = f"CNAME points to {cname_target}, expected {EDGE_HOSTNAME}"
    elif a_records:
        reason = f"A records {a_records} don't match expected {sorted(_expected_a_records())}"
    else:
        reason = "no CNAME or A records found"

    return DomainVerifyOut(
        domain=domain,
        cname_target=cname_target,
        a_records=a_records,
        txt_records=txt_records,
        verified=verified,
        is_verified_db=td.is_verified,
        reason=reason,
    )


# ----------------------------------------------------------------------
# Caddy on-demand TLS ask endpoint
# ----------------------------------------------------------------------
#
# Caddy's on-demand TLS feature calls this URL when an unknown SNI hits
# its listener, asking whether to provision a cert. We return 200 OK if
# the domain is registered with a tenant; 403 otherwise. This is the
# single point of authority for "is this hostname allowed on our edge."

@router.get("/caddy/ask")
async def caddy_ask(domain: str, db: AsyncSession = Depends(get_db)):
    """Public endpoint (no auth — Caddy is unauthenticated by design).

    Authoritative answer for Caddy's `on_demand_tls.ask` URL. The
    response code matters; the body is informational.
        200 → Caddy may issue a cert for this domain.
        403 → refuse.
    """
    d = (domain or "").lower().strip()
    if not d:
        raise HTTPException(status_code=400, detail="missing domain")

    # Block IP addresses / nonsense — only allow real hostnames.
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", d):
        raise HTTPException(status_code=403, detail="invalid hostname")

    row = (await db.execute(
        select(TenantDomain).where(TenantDomain.domain == d)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=403, detail="domain not registered")
    if not row.is_verified:
        # DNS not yet pointed at us → refuse to issue a cert. Prevents
        # an attacker from CNAMEing arbitrary domains to us, registering
        # them, and exhausting our Let's Encrypt rate limit.
        raise HTTPException(status_code=403, detail="domain not verified")

    tenant = (await db.execute(select(Tenant).where(Tenant.id == row.tenant_id))).scalar_one_or_none()
    if not tenant or tenant.status != "active":
        raise HTTPException(status_code=403, detail="tenant inactive")

    return {"ok": True, "tenant_id": row.tenant_id}


# ============================================================
# Outbound audit digest — manual trigger
# ============================================================

@router.post("/outbound-digest/send")
async def trigger_outbound_digest(
    hours: int = 24,
    recipient: Optional[str] = None,
    force: bool = True,
    _: User = Depends(require_super_admin),
):
    """Manually fire the outbound audit digest right now.

    Defaults: last 24 hours, to steve@aamp.agency. Pass `?hours=168`
    to get a weekly retrospective; pass `?recipient=...` to redirect.
    The digest also auto-fires daily via the background loop in main.py.

    `force=True` (default for manual triggers) bypasses the 18h dedup
    check so super_admin can re-fire on demand.
    """
    from app.services.outbound_digest import send_digest, DIGEST_RECIPIENT
    result = await send_digest(hours=hours, recipient=recipient or DIGEST_RECIPIENT, force=force)
    return result


# ============================================================
# Engagement-engine cost view — separate from /api/admin/costs which
# reads credit_ledger (platform-wide vendor cost). This view reads the
# engagement engine's own counters: actions.ai_generation_cost_usd,
# signals.ai_scoring_cost_usd, and rolls them up against the
# per-tenant budget in tenant_ai_config.
#
# The split matters because BYO-AI tenants will eventually pay their own
# Anthropic/OpenAI bills directly; the engine cost is what THEY see
# on their LLM provider invoice. credit_ledger cost is what BMP eats
# on behalf of all customers (transport + tooling that BMP can't
# offload to the customer's account).
# ============================================================

async def _provision_engagement_engine_scaffolding(
    db: AsyncSession, *,
    tenant_id: int, tenant_name: str,
    created_by_user_id: int | None,
) -> None:
    """One-shot per-tenant engagement-engine scaffolding.

    Idempotent — every INSERT uses ON CONFLICT DO NOTHING or a NOT EXISTS
    guard so re-running on an established tenant is safe.

    Creates:
      1. tenant_ai_config row with sensible defaults (aamp_default provider,
         Anthropic Haiku/Sonnet model picks, per-engagement budget $5,
         no monthly cap by default).
      2. playbooks row mirroring DEFAULT_30DAY_TEMPLATE — the canonical
         13-step cadence. Without this, engagements get current_playbook_id=NULL
         and analytics lose the linkage.
      3. sequence_templates row for legacy rollback compatibility (the
         legacy sequence_engine looks it up via SequenceTemplate.is_default).
      4. email_identities placeholder row so the EmailChannel adapter
         finds a sender record. sender_email starts NULL — tenant fills
         it in via the CRM settings page; until then the engine falls
         back to the assigned BDR's get_sender_info() derivation.
    """
    from sqlalchemy import text as _sa_text
    import json as _json

    # 1. tenant_ai_config — Anthropic Haiku for fast classifies, Sonnet
    # for higher-stakes decisions. $5 per-engagement default catches the
    # common runaway: one over-budget engagement before it impacts others.
    await db.execute(_sa_text("""
        INSERT INTO tenant_ai_config (
            tenant_id, provider,
            model_signal_scoring, model_reply_classification,
            model_content_generation, model_decision_making,
            model_engagement_summary,
            per_engagement_budget_usd,
            tcpa_b2b_override, default_timezone,
            created_at, updated_at
        )
        VALUES (
            :t, 'aamp_default',
            'claude-haiku-4-5', 'claude-haiku-4-5',
            'claude-sonnet-4-6', 'claude-sonnet-4-6',
            'claude-haiku-4-5',
            5.00,
            FALSE, 'America/New_York',
            NOW(), NOW()
        )
        ON CONFLICT (tenant_id) DO NOTHING
    """), {"t": tenant_id})

    # 2. Canonical playbook from DEFAULT_30DAY_TEMPLATE. We store the
    # template steps inside ai_strategy_json so callers that need to
    # inspect the plan don't have to import sequence_engine.
    from app.services.sequence_engine import DEFAULT_30DAY_TEMPLATE
    playbook_strategy = _json.dumps({
        "imported_via": "tenant_create_scaffolding",
        "steps": DEFAULT_30DAY_TEMPLATE,
    })
    await db.execute(_sa_text("""
        INSERT INTO playbooks (
            tenant_id, name, description, phase, mode,
            ai_strategy_json, is_active, version,
            created_by_user_id, created_at, updated_at
        )
        SELECT :t, '30-day default',
               'Default 13-step multi-channel cadence (email + iMessage + call + LinkedIn)',
               'cold_outreach', 'linear_sequence',
               CAST(:strategy AS jsonb), TRUE, 1,
               :user_id, NOW(), NOW()
        WHERE NOT EXISTS (
            SELECT 1 FROM playbooks
            WHERE tenant_id = :t AND name = '30-day default'
        )
    """), {
        "t": tenant_id,
        "strategy": playbook_strategy,
        "user_id": created_by_user_id,
    })

    # 3. Legacy sequence_templates row for rollback compatibility. The
    # legacy `start_sequence_from_template` looks up by is_default+is_active.
    # auto_skip_days / auto_resume_days are NOT NULL on prod schema —
    # 0/0 means "never auto-pause / never auto-resume" (engine default).
    #
    # sequence_templates.name carries a GLOBAL unique constraint on prod
    # (not per-tenant). Until that's migrated to (tenant_id, name), we
    # suffix the template name with the tenant_id so each tenant gets
    # their own row that the legacy lookup still finds (it filters by
    # tenant_id auto-filter, then by is_default — so the name only has
    # to be globally unique on disk, not per-tenant).
    legacy_steps_json = _json.dumps(DEFAULT_30DAY_TEMPLATE)
    legacy_template_name = f"30-day default (tenant {tenant_id})"
    await db.execute(_sa_text("""
        INSERT INTO sequence_templates (
            tenant_id, name, steps_json,
            is_default, is_active, created_by,
            auto_skip_days, auto_resume_days,
            created_at, updated_at
        )
        SELECT :t, :name, :steps,
               TRUE, TRUE, :user_id,
               0, 0,
               NOW(), NOW()
        WHERE NOT EXISTS (
            SELECT 1 FROM sequence_templates
            WHERE tenant_id = :t AND is_default = TRUE
        )
    """), {
        "t": tenant_id, "name": legacy_template_name,
        "steps": legacy_steps_json,
        "user_id": created_by_user_id,
    })

    # 4. email_identities placeholder. is_active=FALSE so the warmup-cap
    # guard in EmailChannel falls through to the BDR-fallback sender
    # derivation (get_sender_info) until the tenant configures their
    # real sender email via the CRM settings page.
    #
    # sender_email AND domain are NOT NULL on prod schema. Until the
    # tenant fills these in, we stamp a slug-anchored placeholder under
    # the auto-provisioned go.{slug}.leadprospector.ai domain — these
    # never actually send because is_active=FALSE.
    slug_domain = f"go.tenant{tenant_id}.placeholder"
    placeholder_email = f"noreply@{slug_domain}"
    await db.execute(_sa_text("""
        INSERT INTO email_identities (
            tenant_id, sender_name, sender_email, domain,
            daily_send_cap, sent_today, sent_today_date, reset_timezone,
            warmup_stage, is_active,
            created_at, updated_at
        )
        SELECT :t, :name, :email, :domain,
               50, 0, CURRENT_DATE, 'America/New_York',
               'new', FALSE,
               NOW(), NOW()
        WHERE NOT EXISTS (
            SELECT 1 FROM email_identities WHERE tenant_id = :t
        )
    """), {
        "t": tenant_id, "name": tenant_name[:120],
        "email": placeholder_email, "domain": slug_domain,
    })


async def _engagement_costs_summary(*, days: int, db: AsyncSession) -> dict:
    """Per-tenant engagement-engine AI burn for the last N days, with
    budget status.

    For each tenant returns:
      - actions_cost_usd:        sum(actions.ai_generation_cost_usd) in window
      - signals_cost_usd:        sum(signals.ai_scoring_cost_usd) in window
      - engagement_running_usd:  sum(engagements.monthly_ai_cost_usd) — the
                                 engine's running per-month counter, NOT
                                 limited to the requested window
      - monthly_budget_usd:      tenant_ai_config.monthly_budget_usd (NULL → uncapped)
      - current_month_spent_usd: tenant_ai_config.current_month_spent_usd
      - budget_pct_used:         current/budget * 100 (NULL when no budget)
      - over_budget:             bool

    Sorted by total spend descending.
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import text as _sa_text

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=max(1, min(days, 365)))

    rows = (await db.execute(_sa_text("""
        WITH
        action_costs AS (
            SELECT tenant_id,
                   COALESCE(SUM(ai_generation_cost_usd), 0) AS usd,
                   COUNT(*) AS events
            FROM actions
            WHERE created_at >= :since
            GROUP BY tenant_id
        ),
        signal_costs AS (
            SELECT tenant_id,
                   COALESCE(SUM(ai_scoring_cost_usd), 0) AS usd,
                   COUNT(*) AS events
            FROM signals
            WHERE observed_at >= :since
            GROUP BY tenant_id
        ),
        eng_running AS (
            SELECT tenant_id,
                   COALESCE(SUM(monthly_ai_cost_usd), 0) AS usd,
                   COUNT(*) AS active_engagements
            FROM engagements
            WHERE status = 'active'
            GROUP BY tenant_id
        )
        SELECT t.id AS tenant_id,
               t.name AS tenant_name,
               COALESCE(a.usd, 0)    AS actions_cost_usd,
               COALESCE(a.events, 0) AS actions_count,
               COALESCE(s.usd, 0)    AS signals_cost_usd,
               COALESCE(s.events, 0) AS signals_count,
               COALESCE(r.usd, 0)    AS engagement_running_usd,
               COALESCE(r.active_engagements, 0) AS active_engagements,
               c.monthly_budget_usd,
               c.per_engagement_budget_usd,
               c.current_month_spent_usd,
               c.current_month_reset_at,
               c.provider AS llm_provider
        FROM tenants t
        LEFT JOIN action_costs a    ON a.tenant_id = t.id
        LEFT JOIN signal_costs s    ON s.tenant_id = t.id
        LEFT JOIN eng_running r     ON r.tenant_id = t.id
        LEFT JOIN tenant_ai_config c ON c.tenant_id = t.id
        ORDER BY (
            COALESCE(a.usd, 0) + COALESCE(s.usd, 0)
        ) DESC, t.id
    """), {"since": window_start})).fetchall()

    breakdown = []
    grand_actions = 0.0
    grand_signals = 0.0
    grand_running = 0.0

    for r in rows:
        actions_usd = float(r.actions_cost_usd or 0)
        signals_usd = float(r.signals_cost_usd or 0)
        running_usd = float(r.engagement_running_usd or 0)
        budget     = float(r.monthly_budget_usd) if r.monthly_budget_usd is not None else None
        spent      = float(r.current_month_spent_usd or 0)
        pct        = (spent / budget * 100.0) if (budget and budget > 0) else None
        over       = bool(budget and budget > 0 and spent > budget)
        grand_actions += actions_usd
        grand_signals += signals_usd
        grand_running += running_usd
        breakdown.append({
            "tenant_id":              int(r.tenant_id),
            "tenant_name":            r.tenant_name,
            "llm_provider":           r.llm_provider,
            "window_actions_usd":     round(actions_usd, 6),
            "window_actions_count":   int(r.actions_count or 0),
            "window_signals_usd":     round(signals_usd, 6),
            "window_signals_count":   int(r.signals_count or 0),
            "window_total_usd":       round(actions_usd + signals_usd, 6),
            "engagement_running_usd": round(running_usd, 6),
            "active_engagements":     int(r.active_engagements or 0),
            "monthly_budget_usd":     budget,
            "per_engagement_budget_usd": (
                float(r.per_engagement_budget_usd)
                if r.per_engagement_budget_usd is not None else None
            ),
            "current_month_spent_usd":  round(spent, 6),
            "budget_pct_used":          round(pct, 1) if pct is not None else None,
            "over_budget":              over,
            "month_resets_at":          (
                r.current_month_reset_at.isoformat()
                if r.current_month_reset_at else None
            ),
        })

    # Tenants currently over budget surface for easy alerting.
    over_budget_tenants = [b for b in breakdown if b["over_budget"]]

    return {
        "window_days":      days,
        "window_start":     window_start.isoformat(),
        "generated_at":     now.isoformat(),
        "totals": {
            "actions_usd":  round(grand_actions, 6),
            "signals_usd":  round(grand_signals, 6),
            "window_total_usd": round(grand_actions + grand_signals, 6),
            "engagement_running_usd": round(grand_running, 6),
            "tenants_counted": len(breakdown),
            "tenants_over_budget": len(over_budget_tenants),
        },
        "by_tenant":           breakdown,
        "over_budget_tenants": over_budget_tenants,
    }


@router.get("/engagement-costs")
async def engagement_costs(
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """Per-tenant engagement-engine AI burn for the last N days, with
    budget status. Public super_admin endpoint; delegates to
    _engagement_costs_summary which is also reused by the per-tenant
    detail view."""
    return await _engagement_costs_summary(days=days, db=db)


@router.get("/tenants/{tenant_id}/engagement-cost")
async def tenant_engagement_cost(
    tenant_id: int,
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """Single-tenant engagement-engine cost view — used by the tenant
    detail panel. Same shape as one entry in /admin/engagement-costs."""
    summary = await _engagement_costs_summary(days=days, db=db)
    match = next((b for b in summary["by_tenant"] if b["tenant_id"] == tenant_id), None)
    if not match:
        from fastapi import HTTPException
        raise HTTPException(404, f"tenant {tenant_id} not found")
    return {
        "window_days":  summary["window_days"],
        "window_start": summary["window_start"],
        "generated_at": summary["generated_at"],
        "tenant":       match,
    }
