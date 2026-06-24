"""
Resolves which calendar a BDR's outbound assets should book against.

Each user has an optional `default_booking_host_id`. When set, anything
that emits a "Schedule a meeting" link on behalf of that user — email
signature, sidebar deep-link, Chrome extension button — should route to
the host's calendar instead of the user's own. Lets an admin centralize
demos / discovery calls onto one calendar without making every BDR a
Google Calendar owner.

When the host is unset, or the host is inactive / has no connected
calendar, we fall back to the user themselves.
"""
from __future__ import annotations
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.config import settings


async def resolve_booking_host(db: AsyncSession, user: User) -> User:
    """Return the User whose calendar this user's booking links should
    target. Always returns SOMETHING — falls back to `user` if the
    configured host is missing/inactive."""
    host_id = getattr(user, "default_booking_host_id", None)
    if not host_id or host_id == user.id:
        return user
    host = (await db.execute(
        select(User).where(User.id == int(host_id), User.is_active == True)
    )).scalar_one_or_none()
    if not host:
        return user
    return host


async def resolve_booking_url(db: AsyncSession, user: User) -> str:
    """The booking URL we should show on this user's outbound assets.
    Priority:
      1. Native /book/{slug} on the routed host (if they've connected
         Google Calendar AND have a booking slug).
      2. The user's own scheduling_url override.
      3. The org-level iClosed fallback.
    Returns "" only when nothing is configured anywhere."""
    host = await resolve_booking_host(db, user)
    if host.booking_slug and host.google_refresh_token:
        # Use the booking host's OWN tenant domain (the /book/{slug} page is
        # served app-wide), not the BMP-global settings.schedule_public_url —
        # otherwise a white-label tenant's signature links to
        # schedule.backyardmarketingpros.com.
        from app.tenancy import tenant_primary_base_url
        base = await tenant_primary_base_url(db, host.tenant_id)
        if not base:
            base = (settings.schedule_public_url or "").rstrip("/")
        base = (base or "").rstrip("/")
        if base:
            return f"{base}/book/{host.booking_slug}"
    own = (user.scheduling_url or "").strip()
    if own:
        return own
    return (settings.iclosed_booking_url or "").strip()
