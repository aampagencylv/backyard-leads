"""
Google OAuth — connect / disconnect / status.

Phase 1 of the native scheduler build. The actual booking page +
availability rules + slot generator come in subsequent commits; this
ships only the auth dance + Google Calendar handshake so each user can
authorize the platform to read their primary calendar (free/busy) and
write events to a dedicated "BMP Discovery Calls" calendar.

Endpoints:
  GET  /api/google/oauth/start       — issues redirect to Google consent
  GET  /api/google/oauth/callback    — handles ?code= → stores refresh_token
  GET  /api/google/oauth/status      — JSON for the Settings UI
  POST /api/google/oauth/disconnect  — revoke + clear stored tokens

Security: the OAuth state parameter carries a short-lived JWT signed
with our SECRET_KEY. Without that, the callback can't be tied back to
the rep who initiated the flow (and worse, anyone could send a victim
to the start URL → callback would attribute the connection to them).
"""
from __future__ import annotations
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ALGORITHM, SECRET_KEY, get_current_user
from app.config import settings
from app.tenancy import get_tenant_db
from app.database import get_db
from app.models import User
from app.services.google_oauth import (
    GoogleAPIError,
    build_auth_url,
    exchange_code_for_tokens,
    get_userinfo,
    is_configured,
    list_calendars,
    refresh_access_token,
    revoke_token,
)

log = logging.getLogger("bmp.google_oauth_routes")

router = APIRouter(prefix="/api/google/oauth", tags=["google-oauth"])


# ============================================================
# State JWT — round-trips user_id through the OAuth dance
# ============================================================

STATE_TTL_MINUTES = 10


def _mint_state(user_id: int, origin: str | None = None) -> str:
    """Sign a short-lived JWT carrying the connecting user's id, the origin
    host they started from, and a nonce. Google echoes this back on the
    callback; we verify before persisting tokens so an attacker can't trick
    us into binding their Google account to someone else's user record.

    `origin` (e.g. https://aamp.leadprospector.ai) lets the callback — which
    Google always sends to the single registered redirect_uri (BMP host) —
    bounce the user back to the tenant they actually came from."""
    claims = {
        "sub": str(user_id),
        "purpose": "google_oauth",
        "nonce": secrets.token_urlsafe(8),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=STATE_TTL_MINUTES),
    }
    if origin:
        claims["origin"] = origin
    return jwt.encode(claims, SECRET_KEY, algorithm=ALGORITHM)


def _verify_state(state: str) -> tuple[int, str | None]:
    try:
        payload = jwt.decode(state, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Invalid OAuth state: {e}")
    if payload.get("purpose") != "google_oauth":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="State purpose mismatch")
    return int(payload["sub"]), payload.get("origin")


def _slugify_for_booking(first: str, last: str) -> str:
    raw = f"{first}-{last}".strip("-").lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return raw or "booking"


async def _ensure_unique_slug(db: AsyncSession, base: str, user_id: int) -> str:
    """Return `base` if free, else append -2, -3, ... until unique."""
    candidate = base
    suffix = 2
    while True:
        existing = (await db.execute(
            select(User).where(User.booking_slug == candidate, User.id != user_id)
        )).scalar_one_or_none()
        if existing is None:
            return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


# ============================================================
# Endpoints
# ============================================================

def _safe_origin(request: Request) -> str | None:
    """The tenant host the user started from, as https://<host>, but only
    if it's one of our own domains (prevents an attacker from smuggling an
    open-redirect target through the signed state). We trust hosts under
    leadprospector.ai and the BMP legacy hosts; anything else → None (the
    callback then falls back to settings.public_url)."""
    host = (request.headers.get("host") or "").split(":", 1)[0].strip().lower()
    if not host:
        return None
    ok = host == "leadprospector.ai" or host.endswith(".leadprospector.ai") \
        or host.endswith("backyardmarketingpros.com")
    return f"https://{host}" if ok else None


@router.get("/start-url")
async def start_google_oauth(request: Request, user: User = Depends(get_current_user)):
    """Return the Google consent URL as JSON. The frontend then sets
    window.location.href to it. We don't return a 30x redirect because
    the browser would strip the Authorization header on the cross-
    origin hop, breaking auth on the way in."""
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth not configured on the server (missing client id / secret)",
        )
    state = _mint_state(user.id, origin=_safe_origin(request))
    return {"auth_url": build_auth_url(state)}


@router.get("/callback")
async def google_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Google redirects here after consent. We:
      1. Verify the state JWT to learn which user authorized
      2. Exchange code → tokens
      3. Fetch userinfo to learn which Google email was authorized
      4. Find or create the dedicated 'BMP Discovery Calls' calendar
      5. Persist refresh_token + calendar_id + booking_slug
      6. Redirect back to /#settings with a success/failure flag
    """
    public_url = settings.public_url.rstrip("/")
    # Use query-param + #settings hash. The SPA's _checkGoogleOAuthRedirect
    # parses location.search and calls showPage('settings'); the hash is
    # cosmetic for landing-page polish. Earlier we used hash-only, but
    # there's no hashchange→page router so users landed on Dashboard
    # after consent and never saw the connection state update.
    fail_redirect = f"{public_url}/?google_oauth=error"
    success_redirect = f"{public_url}/?google_oauth=connected#calendar"

    if error:
        log.warning(f"Google OAuth user-denied or error: {error}")
        return RedirectResponse(f"{fail_redirect}&reason={error}", status_code=302)
    if not (code and state):
        return RedirectResponse(f"{fail_redirect}&reason=missing_code", status_code=302)

    try:
        user_id, origin = _verify_state(state)
    except HTTPException:
        return RedirectResponse(f"{fail_redirect}&reason=bad_state", status_code=302)

    # Land the user back on the tenant they started from (the state's origin),
    # not the BMP-global public_url. Google always returns to the single
    # registered redirect_uri (BMP host); origin carries the real tenant.
    if origin:
        base = origin.rstrip("/")
        fail_redirect = f"{base}/?google_oauth=error"
        success_redirect = f"{base}/?google_oauth=connected#calendar"

    # Untenanted lookup (db is get_db): the user is authenticated by the
    # signed state, and the callback runs on the BMP host — a tenant-scoped
    # session would fail to find a non-BMP-tenant user's row.
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        return RedirectResponse(f"{fail_redirect}&reason=unknown_user", status_code=302)

    try:
        tokens = await exchange_code_for_tokens(code)
        if not tokens.refresh_token:
            # Google omits refresh_token if user previously consented.
            # We force prompt=consent in build_auth_url specifically to
            # avoid this; surfacing it as an error is the right move
            # rather than persisting a useless connection.
            return RedirectResponse(f"{fail_redirect}&reason=no_refresh_token", status_code=302)

        info = await get_userinfo(tokens.access_token)
    except GoogleAPIError as e:
        log.exception(f"Google OAuth callback failed: {e}")
        return RedirectResponse(f"{fail_redirect}&reason=google_api", status_code=302)

    user.google_email = info.email
    user.google_refresh_token = tokens.refresh_token
    # Default to PRIMARY. Reps can switch to any calendar they own via
    # the dropdown in Settings → Google Calendar (uses calendar.readonly
    # to list their calendars + the existing token to write events).
    # We deliberately don't auto-create a dedicated "BMP Bookings"
    # calendar — that requires the broader calendar.app.created scope,
    # and most users would rather keep events on their primary calendar
    # anyway. If they want a dedicated calendar, they create one in
    # Google Calendar and pick it from our dropdown.
    user.google_calendar_id = "primary"
    user.google_connected_at = datetime.now(timezone.utc)
    if not user.booking_slug:
        slug_base = _slugify_for_booking(user.first_name, user.last_name)
        user.booking_slug = await _ensure_unique_slug(db, slug_base, user.id)
    await db.commit()

    return RedirectResponse(success_redirect, status_code=302)


@router.get("/status")
async def google_oauth_status(request: Request, user: User = Depends(get_current_user)):
    """JSON snapshot for the Settings UI."""
    # Build the booking URL on the TENANT's own host (the /book/{slug} page
    # is served app-wide on every host), not the BMP-global
    # settings.schedule_public_url — otherwise a white-label tenant's public
    # booking link reads schedule.backyardmarketingpros.com.
    host = (request.headers.get("host") or "").split(":", 1)[0].strip()
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    base = f"{scheme}://{host}" if host else settings.schedule_public_url.rstrip("/")
    return {
        "configured": is_configured(),
        "connected": bool(user.google_refresh_token),
        "google_email": user.google_email,
        "calendar_id": user.google_calendar_id,
        "booking_slug": user.booking_slug,
        "connected_at": user.google_connected_at.isoformat() if user.google_connected_at else None,
        "booking_url": (
            f"{base}/book/{user.booking_slug}"
            if user.booking_slug else None
        ),
    }


@router.get("/calendars")
async def list_my_calendars(user: User = Depends(get_current_user)):
    """Return the user's calendar list — used by the Settings dropdown
    to let reps pick which calendar booked events should land on.
    Filters to calendars where the user has writer/owner access (no
    point listing read-only shared calendars they can't write to)."""
    if not user.google_refresh_token:
        raise HTTPException(status_code=400, detail="Google not connected")
    try:
        tokens = await refresh_access_token(user.google_refresh_token)
        items = await list_calendars(tokens.access_token)
    except GoogleAPIError as e:
        log.warning(f"list_my_calendars failed for user {user.id}: {e}")
        raise HTTPException(status_code=502, detail=f"Google API error: {e.status}")
    out = []
    for c in items:
        access = (c.get("accessRole") or "").lower()
        if access not in ("writer", "owner"):
            continue
        cal_id = c.get("id")
        out.append({
            "id": cal_id,
            "summary": c.get("summary") or cal_id,
            "primary": bool(c.get("primary")),
            "background_color": c.get("backgroundColor"),
            "selected": cal_id == (user.google_calendar_id or "primary"),
        })
    # Always include "primary" as a synthetic first option even if
    # Google's calendarList didn't return it (rare but possible when
    # the user has hidden their primary from the list).
    if not any(c["id"] == "primary" or c["primary"] for c in out):
        out.insert(0, {
            "id": "primary",
            "summary": f"Primary ({user.google_email or user.email})",
            "primary": True,
            "background_color": None,
            "selected": (user.google_calendar_id or "primary") == "primary",
        })
    return {"calendars": out, "selected_id": user.google_calendar_id or "primary"}


from pydantic import BaseModel


class _SetCalendarRequest(BaseModel):
    calendar_id: str


@router.patch("/calendar")
async def set_my_booking_calendar(
    body: _SetCalendarRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Update which calendar new bookings land on. We only validate
    the picked id against the user's own calendar list, so a rep can't
    point bookings at someone else's calendar."""
    if not user.google_refresh_token:
        raise HTTPException(status_code=400, detail="Google not connected")
    target = (body.calendar_id or "").strip() or "primary"
    if target != "primary":
        try:
            tokens = await refresh_access_token(user.google_refresh_token)
            items = await list_calendars(tokens.access_token)
        except GoogleAPIError as e:
            raise HTTPException(status_code=502, detail=f"Google API error: {e.status}")
        owned = {c.get("id") for c in items
                 if (c.get("accessRole") or "").lower() in ("writer", "owner")}
        if target not in owned:
            raise HTTPException(status_code=400,
                                detail="Calendar not in your list (or you don't have write access)")
    user.google_calendar_id = target
    await db.commit()
    return {"calendar_id": target}


@router.post("/disconnect")
async def google_oauth_disconnect(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Best-effort revoke at Google + clear stored tokens. Booking
    slug is retained so we don't break old booking URLs the user may
    have shared (page just shows a 'reconnect' message)."""
    if user.google_refresh_token:
        try:
            await revoke_token(user.google_refresh_token)
        except Exception:
            pass
    user.google_refresh_token = None
    user.google_calendar_id = None
    user.google_email = None
    user.google_connected_at = None
    await db.commit()
    return {"disconnected": True}
