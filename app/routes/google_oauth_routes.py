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
from app.database import get_db
from app.models import User
from app.services.google_oauth import (
    GoogleAPIError,
    build_auth_url,
    exchange_code_for_tokens,
    find_or_create_bookings_calendar,
    get_userinfo,
    is_configured,
    revoke_token,
)

log = logging.getLogger("bmp.google_oauth_routes")

router = APIRouter(prefix="/api/google/oauth", tags=["google-oauth"])


# ============================================================
# State JWT — round-trips user_id through the OAuth dance
# ============================================================

STATE_TTL_MINUTES = 10


def _mint_state(user_id: int) -> str:
    """Sign a short-lived JWT carrying the connecting user's id + a
    nonce. Google echoes this back on the callback; we verify before
    persisting tokens so an attacker can't trick us into binding their
    Google account to someone else's user record."""
    return jwt.encode(
        {
            "sub": str(user_id),
            "purpose": "google_oauth",
            "nonce": secrets.token_urlsafe(8),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=STATE_TTL_MINUTES),
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def _verify_state(state: str) -> int:
    try:
        payload = jwt.decode(state, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Invalid OAuth state: {e}")
    if payload.get("purpose") != "google_oauth":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="State purpose mismatch")
    return int(payload["sub"])


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

@router.get("/start-url")
async def start_google_oauth(user: User = Depends(get_current_user)):
    """Return the Google consent URL as JSON. The frontend then sets
    window.location.href to it. We don't return a 30x redirect because
    the browser would strip the Authorization header on the cross-
    origin hop, breaking auth on the way in."""
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth not configured on the server (missing client id / secret)",
        )
    state = _mint_state(user.id)
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
    success_redirect = f"{public_url}/?google_oauth=connected#settings"

    if error:
        log.warning(f"Google OAuth user-denied or error: {error}")
        return RedirectResponse(f"{fail_redirect}&reason={error}", status_code=302)
    if not (code and state):
        return RedirectResponse(f"{fail_redirect}&reason=missing_code", status_code=302)

    try:
        user_id = _verify_state(state)
    except HTTPException:
        return RedirectResponse(f"{fail_redirect}&reason=bad_state", status_code=302)

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
        cal_id = await find_or_create_bookings_calendar(tokens.access_token)
    except GoogleAPIError as e:
        log.exception(f"Google OAuth callback failed: {e}")
        return RedirectResponse(f"{fail_redirect}&reason=google_api", status_code=302)

    user.google_email = info.email
    user.google_refresh_token = tokens.refresh_token
    user.google_calendar_id = cal_id
    user.google_connected_at = datetime.now(timezone.utc)
    if not user.booking_slug:
        slug_base = _slugify_for_booking(user.first_name, user.last_name)
        user.booking_slug = await _ensure_unique_slug(db, slug_base, user.id)
    await db.commit()

    return RedirectResponse(success_redirect, status_code=302)


@router.get("/status")
async def google_oauth_status(user: User = Depends(get_current_user)):
    """JSON snapshot for the Settings UI."""
    return {
        "configured": is_configured(),
        "connected": bool(user.google_refresh_token),
        "google_email": user.google_email,
        "calendar_id": user.google_calendar_id,
        "booking_slug": user.booking_slug,
        "connected_at": user.google_connected_at.isoformat() if user.google_connected_at else None,
        "booking_url": (
            f"{settings.public_url.rstrip('/')}/book/{user.booking_slug}"
            if user.booking_slug else None
        ),
    }


@router.post("/disconnect")
async def google_oauth_disconnect(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
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
