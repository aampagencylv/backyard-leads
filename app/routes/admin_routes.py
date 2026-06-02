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
                      description="URL-safe identifier, becomes {slug}.agencyprospector.com")
    plan: str = "starter"


class TenantPatch(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None  # active | suspended
    plan: Optional[str] = None


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


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

    # Seed a RuntimeConfig row so every per-tenant accessor (Twilio creds,
    # brand colors, send window, pipeline stages) finds something on first
    # call instead of upserting a row at the wrong moment in a request.
    # The brand_company_name defaults to the tenant's display name so the
    # first cold email out the door for them already says their name.
    db.add(RuntimeConfig(
        tenant_id=tenant.id,
        brand_company_name=tenant.name[:120],
    ))
    await db.commit()
    await db.refresh(tenant)
    await record_audit(db, actor=actor, action="tenant_created",
                       target_type="tenant", target_id=tenant.id,
                       metadata={"name": tenant.name, "slug": tenant.slug})
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
    }


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
#      require CNAME → edge.agencyprospector.com OR an A record matching
#      our VPS IP (env: PLATFORM_EDGE_IP).
#   2. Optional ownership TXT _acmeagency-verify.<domain> = <expected>
#      where <expected> is derived from the tenant id + a salt. Lets
#      large customers prove control without DNS pointing yet.
#
# Caddy on-demand TLS will refuse to issue certs for unverified domains
# (the /caddy/ask endpoint below returns 403 unless the domain is in
# tenant_domains AND its is_verified flag is true).

EDGE_HOSTNAME = "edge.agencyprospector.com"


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
        ans = resolver.resolve(f"_agencyprospector.{domain}", "TXT")
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
