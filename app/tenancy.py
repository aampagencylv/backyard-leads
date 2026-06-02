"""
Multi-tenant request context resolution.

Resolution order (first hit wins):
  1. JWT `tenant_id` claim (set at login; the user's home tenant)
  2. Host header → tenant_domains lookup (custom / white-label domain)
  3. Host header → `{slug}.leadprospector.ai` → tenants.slug
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
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, with_loader_criteria

from app.config import settings
from app.database import async_session, get_db
from app.models import Tenant, TenantDomain, TenantMixin

log = logging.getLogger("bmp.tenancy")

# Subdomain suffix used for first-party platform hosting:
#   acmeagency.leadprospector.ai  →  slug='acmeagency'
PLATFORM_DOMAIN_SUFFIX = ".leadprospector.ai"

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

        # {slug}.leadprospector.ai
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


async def get_tenant_db(request: Request):
    """FastAPI dependency: yields a tenant-scoped DB session.

    Activates two layers of tenant isolation for any route that uses it:

    1. **ORM-layer auto-filter** (primary enforcement today). Sets
       `session.info["tenant_id"]`, which the `do_orm_execute` hook
       installed in `install_tenant_filter()` reads. Every ORM SELECT
       that touches a TenantMixin-derived model is auto-rewritten to
       include `WHERE tenant_id = :tid`. Routes need no change beyond
       adopting this dep — existing queries are transparently scoped.

    2. **Postgres GUC + RLS** (defense-in-depth, dormant). Issues
       `SET app.current_tenant_id = N` on the underlying connection,
       which the RLS `tenant_isolation` policies on every tenant-owned
       table will read. Today the policies are no-ops because the
       `postgres` connection role has `rolbypassrls = true` (Supabase
       default); when we migrate to a non-bypassing role, RLS becomes
       a hard second line of defense behind the ORM filter.

    The GUC is session-scoped (survives mid-handler commit()). On dep
    teardown we RESET it so a pooled connection comes back clean.

    Routes migrate by replacing
        db: AsyncSession = Depends(get_db)
    with
        db: AsyncSession = Depends(get_tenant_db)
    """
    async with async_session() as session:
        tid = await _resolve_tenant_id(request, session)
        # Cache on request.state so downstream deps reuse the resolved id.
        request.state.tenant_id = tid

        # ORM-layer enforcement: the do_orm_execute hook reads this.
        session.info["tenant_id"] = tid

        # GUC for future RLS enforcement (no-op while role bypasses RLS).
        await session.execute(text(f"SET app.current_tenant_id = '{int(tid)}'"))
        try:
            yield session
        finally:
            try:
                await session.execute(text("RESET app.current_tenant_id"))
            except Exception:
                log.exception("RESET app.current_tenant_id failed (tid=%s)", tid)


# ----------------------------------------------------------------------
# ORM-layer auto-filter
# ----------------------------------------------------------------------
#
# A single global `do_orm_execute` listener that rewrites every ORM
# SELECT to include `WHERE tenant_id = :tid` for any entity that
# inherits TenantMixin — but ONLY when the session has a tenant_id
# stamped on `session.info`. Sessions from plain `get_db` have no
# tenant_id stamp and pass through unchanged.
#
# Why this design vs. WHERE-clauses in every query:
#   - Touch-zero migration: a route switches dep, queries unchanged.
#   - Impossible to forget — you can't write a query that bypasses
#     the filter without explicitly clearing session.info["tenant_id"].
#   - Limited to ORM queries; raw `session.execute(text("SELECT ..."))`
#     statements skip this hook. We treat raw SQL as out-of-scope until
#     RLS enforcement (DB role switch) lands.

_FILTER_INSTALLED = False


def install_tenant_filter() -> None:
    """Register the global do_orm_execute hook. Idempotent."""
    global _FILTER_INSTALLED
    if _FILTER_INSTALLED:
        return

    @event.listens_for(Session, "do_orm_execute")
    def _auto_tenant_filter(execute_state):
        # Only filter SELECTs — INSERT/UPDATE/DELETE keep behaving normally.
        if not execute_state.is_select:
            return
        tid = execute_state.session.info.get("tenant_id")
        if tid is None:
            return
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                TenantMixin,
                lambda cls: cls.tenant_id == tid,
                include_aliases=True,
            )
        )

    @event.listens_for(Session, "before_flush")
    def _auto_tenant_stamp(session, flush_context, instances):
        """Stamp tenant_id onto any new TenantMixin instance that doesn't
        have one set. Symmetric with the SELECT auto-filter — routes can
        do `db.add(Company(...))` without touching tenant_id manually
        and the session's resolved tenant gets stamped automatically.

        Only fires when session.info["tenant_id"] is set (tenant-aware
        sessions); legacy get_db sessions still rely on the column
        DEFAULT 1 to backfill.
        """
        tid = session.info.get("tenant_id")
        if tid is None:
            return
        for obj in session.new:
            if not isinstance(obj, TenantMixin):
                continue
            current = getattr(obj, "tenant_id", None)
            if current is None:
                obj.tenant_id = tid

    _FILTER_INSTALLED = True
    log.info("tenant auto-filter installed (do_orm_execute + before_flush hooks)")


from contextlib import contextmanager


@contextmanager
def tenant_scope(session, tenant_id: int):
    """Stamp session.info["tenant_id"] for the duration of the block.

    Use this in background tasks (the sequence engine loop, scheduled
    activations) which run outside any request context and therefore
    have no `get_tenant_db` to set the scope. The same auto-filter /
    auto-stamp hooks apply once `session.info["tenant_id"]` is set.

    Restores the previous value on exit so you can nest these safely.

        async with async_session() as db:
            for tid in active_tenant_ids:
                with tenant_scope(db, tid):
                    await process_pending_steps(db)
    """
    prev = session.info.get("tenant_id")
    session.info["tenant_id"] = int(tenant_id)
    try:
        yield
    finally:
        if prev is None:
            session.info.pop("tenant_id", None)
        else:
            session.info["tenant_id"] = prev


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
