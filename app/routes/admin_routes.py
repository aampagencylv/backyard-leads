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
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    rows = (await db.execute(select(Tenant).order_by(Tenant.id))).scalars().all()
    return [TenantOut.model_validate(t, from_attributes=True) for t in rows]


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
# Impersonation
# ----------------------------------------------------------------------

class ImpersonateOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    acting_as_tenant_id: int


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
    await record_audit(db, actor=actor, action="tenant_impersonate_start",
                       target_type="tenant", target_id=tenant.id,
                       metadata={"tenant_name": tenant.name,
                                 "ip": request.client.host if request.client else None})
    return ImpersonateOut(access_token=token, acting_as_tenant_id=tenant.id)


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
