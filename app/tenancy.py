"""
Multi-tenant request context resolution.

Resolution order (first hit wins):
  1. JWT `tenant_id` claim (set at login; the user's home tenant)
  2. Host header → tenant_domains lookup (custom / white-label domain)
  3. Host header → `{slug}.agencyprospector.com` → tenants.slug
  4. Fall back to tenant 1 (BMP) — preserves single-tenant behavior so
     legacy hosts (prospector.backyardmarketingpros.com etc) keep working

The resolved tenant_id is cached on request.state so dependents don't
re-resolve. Resolution is read-only and never raises — a misrouted
request lands on tenant 1 rather than 500'ing.

For admin impersonation later: a super_admin will be able to pass an
`acting_as_tenant_id` claim in their JWT, and that takes precedence.
"""
from __future__ import annotations
import logging
from typing import Optional

from fastapi import Depends, Request
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Tenant, TenantDomain

log = logging.getLogger("bmp.tenancy")

# Subdomain suffix used for first-party platform hosting:
#   acmeagency.agencyprospector.com  →  slug='acmeagency'
PLATFORM_DOMAIN_SUFFIX = ".agencyprospector.com"

# Hosts considered "BMP legacy" — they fall back to tenant 1 explicitly
# even before tenant_domains is consulted (defense in depth in case the
# seed row gets deleted by mistake).
_LEGACY_BMP_HOSTS = {
    "prospector.backyardmarketingpros.com",
    "audit.backyardmarketingpros.com",
    "schedule.backyardmarketingpros.com",
}


def _normalize_host(raw_host: Optional[str]) -> str:
    """Strip port, lowercase, strip whitespace."""
    if not raw_host:
        return ""
    return raw_host.split(":", 1)[0].strip().lower()


async def _resolve_tenant_id(request: Request, db: AsyncSession) -> int:
    """Find the tenant for this request. Never raises — falls back to 1."""

    # ----- 1. JWT tenant_id claim --------------------------------------
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        try:
            payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
            # Super-admin impersonation: acting_as wins over home tenant
            acting = payload.get("acting_as_tenant_id")
            if isinstance(acting, int) and acting > 0:
                return acting
            tid = payload.get("tenant_id")
            if isinstance(tid, int) and tid > 0:
                return tid
        except JWTError:
            pass  # fall through to host-based resolution

    # ----- 2 & 3. Host header ------------------------------------------
    host = _normalize_host(request.headers.get("host"))
    if host:
        # Fast-path for known BMP hosts
        if host in _LEGACY_BMP_HOSTS:
            return 1

        # Custom domain lookup
        try:
            r = await db.execute(
                select(TenantDomain.tenant_id).where(TenantDomain.domain == host)
            )
            row = r.scalar_one_or_none()
            if row:
                return int(row)
        except Exception:
            log.exception("tenant_domains lookup failed for host=%s", host)

        # {slug}.agencyprospector.com
        if host.endswith(PLATFORM_DOMAIN_SUFFIX):
            slug = host[: -len(PLATFORM_DOMAIN_SUFFIX)].strip()
            if slug:
                try:
                    r = await db.execute(
                        select(Tenant.id).where(Tenant.slug == slug)
                    )
                    row = r.scalar_one_or_none()
                    if row:
                        return int(row)
                except Exception:
                    log.exception("tenant slug lookup failed for slug=%s", slug)

    # ----- 4. Fallback -------------------------------------------------
    return 1


async def get_current_tenant_id(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> int:
    """FastAPI dependency: resolve and cache the tenant for this request."""
    cached = getattr(request.state, "tenant_id", None)
    if isinstance(cached, int) and cached > 0:
        return cached
    tid = await _resolve_tenant_id(request, db)
    request.state.tenant_id = tid
    return tid


def scope_to_tenant(query, model, tenant_id: int):
    """Add `WHERE Model.tenant_id = :tid` to a select() query.

    Standard pattern for tenant-scoped reads:

        from app.tenancy import get_current_tenant_id, scope_to_tenant

        @router.get("/companies")
        async def list_companies(
            tenant_id: int = Depends(get_current_tenant_id),
            db: AsyncSession = Depends(get_db),
        ):
            q = scope_to_tenant(select(Company), Company, tenant_id)
            ...
    """
    return query.where(model.tenant_id == tenant_id)
