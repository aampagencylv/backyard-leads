"""
Google OAuth + Calendar API client.

Pure-httpx implementation — no google-auth-* SDK dependency. Keeps the
deploy lean and matches the rest of our HTTP code style.

Three flows live here:

  1. **OAuth dance** — `build_auth_url`, `exchange_code_for_tokens`,
     `refresh_access_token`. We persist only the long-lived refresh
     token; access tokens are exchanged on demand and never stored.

  2. **Identity** — `get_userinfo` returns the user's Google email so
     we can show "Connected as steve@aamp.agency" in Settings.

  3. **Calendar API** — `list_calendars`, `create_calendar`,
     `list_events`, `create_event`. Used by the native scheduler:
     read primary calendar for free/busy, write booked events to a
     dedicated "BMP Discovery Calls" calendar so disconnecting the
     integration doesn't touch the user's personal events.

Errors raise GoogleAPIError with the upstream status + body. Callers
typically catch and surface a friendly message.
"""
from __future__ import annotations
import logging
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger("bmp.google_oauth")


# Scopes the consent screen asks for. `userinfo.email` lets us identify
# which Google account the user connected; the two calendar scopes let
# us read free/busy and write booking events.
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CAL_BASE = "https://www.googleapis.com/calendar/v3"

# Brand-named calendar we auto-create on first connect. All booked
# discovery calls land here — keeps the user's primary calendar clean
# and means we can revoke access without touching their personal events.
BOOKINGS_CALENDAR_NAME = "BMP Discovery Calls"


class GoogleAPIError(Exception):
    def __init__(self, status: int, body: str | dict):
        super().__init__(f"google_api {status}: {str(body)[:300]}")
        self.status = status
        self.body = body


def is_configured() -> bool:
    return bool(settings.google_oauth_client_id and settings.google_oauth_client_secret)


def redirect_uri() -> str:
    """Where Google sends the user back after consent. MUST match what's
    registered in the Google Cloud Console exactly."""
    return f"{settings.public_url.rstrip('/')}/api/google/oauth/callback"


# ============================================================
# OAuth dance
# ============================================================

def build_auth_url(state: str) -> str:
    """Initiate flow. `state` is an opaque token we round-trip back to
    the callback (we use it to encode the user_id of the rep who
    initiated the connect — signed via secret_key to prevent forgery)."""
    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",       # required to receive refresh_token
        "prompt": "consent",            # force consent so refresh_token is re-issued
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


@dataclass
class GoogleTokens:
    access_token: str
    expires_in: int  # seconds
    refresh_token: Optional[str] = None  # only present on first consent
    id_token: Optional[str] = None
    scope: Optional[str] = None
    token_type: Optional[str] = None


async def exchange_code_for_tokens(code: str) -> GoogleTokens:
    """Trade the one-shot auth code for an access_token + refresh_token."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(TOKEN_URL, data={
            "code": code,
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
        })
    if r.status_code != 200:
        raise GoogleAPIError(r.status_code, r.text)
    body = r.json()
    return GoogleTokens(
        access_token=body["access_token"],
        expires_in=body.get("expires_in", 3600),
        refresh_token=body.get("refresh_token"),
        id_token=body.get("id_token"),
        scope=body.get("scope"),
        token_type=body.get("token_type"),
    )


async def refresh_access_token(refresh_token: str) -> GoogleTokens:
    """Get a fresh access_token from a stored refresh_token. Google
    occasionally rotates the refresh_token itself; if so, the response
    includes a new one and the caller should persist it."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(TOKEN_URL, data={
            "refresh_token": refresh_token,
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "grant_type": "refresh_token",
        })
    if r.status_code != 200:
        raise GoogleAPIError(r.status_code, r.text)
    body = r.json()
    return GoogleTokens(
        access_token=body["access_token"],
        expires_in=body.get("expires_in", 3600),
        refresh_token=body.get("refresh_token") or refresh_token,
        id_token=body.get("id_token"),
        scope=body.get("scope"),
        token_type=body.get("token_type"),
    )


async def revoke_token(token: str) -> bool:
    """Best-effort revoke. Disconnecting the integration calls this so
    Google immediately invalidates our access. Idempotent — already-
    revoked tokens return 200."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            return r.status_code in (200, 400)  # 400 = already revoked
        except httpx.HTTPError:
            return False


# ============================================================
# Identity
# ============================================================

@dataclass
class GoogleUserInfo:
    email: str
    sub: str  # Google's stable user id
    name: Optional[str] = None
    picture: Optional[str] = None


async def get_userinfo(access_token: str) -> GoogleUserInfo:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
    if r.status_code != 200:
        raise GoogleAPIError(r.status_code, r.text)
    body = r.json()
    return GoogleUserInfo(
        email=body.get("email", ""),
        sub=body.get("sub", ""),
        name=body.get("name"),
        picture=body.get("picture"),
    )


# ============================================================
# Calendar API
# ============================================================

async def _cal_request(
    method: str, path: str, *, access_token: str,
    params: Optional[dict] = None, json_body: Optional[dict] = None,
) -> dict:
    url = f"{CAL_BASE}{path}"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.request(
            method, url,
            params=params, json=json_body,
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json"},
        )
    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = r.text
        raise GoogleAPIError(r.status_code, err)
    if not r.text:
        return {}
    return r.json()


async def list_calendars(access_token: str) -> list[dict]:
    """Return the user's calendar list. Each entry: id, summary,
    primary (bool), accessRole."""
    data = await _cal_request("GET", "/users/me/calendarList", access_token=access_token)
    return data.get("items", []) or []


async def find_or_create_bookings_calendar(access_token: str) -> str:
    """Return the calendarId for our brand-named "BMP Discovery Calls"
    calendar, creating it if it doesn't exist. Idempotent."""
    cals = await list_calendars(access_token)
    for c in cals:
        if (c.get("summary") or "").strip() == BOOKINGS_CALENDAR_NAME:
            return c.get("id")
    created = await _cal_request(
        "POST", "/calendars",
        access_token=access_token,
        json_body={
            "summary": BOOKINGS_CALENDAR_NAME,
            "description": (
                "Discovery calls booked through Backyard Marketing Pros' "
                "scheduler. Disconnecting the integration leaves these "
                "events in place."
            ),
        },
    )
    cal_id = created.get("id")
    if not cal_id:
        raise GoogleAPIError(500, "create_calendar returned no id")
    return cal_id


async def list_events(
    access_token: str, calendar_id: str,
    *, time_min: datetime, time_max: datetime,
) -> list[dict]:
    """List events between two timestamps. Used by the slot generator
    to subtract busy time from advertised availability."""
    if time_min.tzinfo is None:
        time_min = time_min.replace(tzinfo=timezone.utc)
    if time_max.tzinfo is None:
        time_max = time_max.replace(tzinfo=timezone.utc)
    data = await _cal_request(
        "GET", f"/calendars/{urllib.parse.quote(calendar_id, safe='')}/events",
        access_token=access_token,
        params={
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 250,
        },
    )
    return data.get("items", []) or []


async def free_busy(
    access_token: str, calendar_ids: list[str],
    *, time_min: datetime, time_max: datetime,
) -> list[tuple[datetime, datetime]]:
    """Free-busy across one or more calendars. Returns the union of
    busy ranges as a list of (start, end) datetime tuples (UTC)."""
    if time_min.tzinfo is None:
        time_min = time_min.replace(tzinfo=timezone.utc)
    if time_max.tzinfo is None:
        time_max = time_max.replace(tzinfo=timezone.utc)
    data = await _cal_request(
        "POST", "/freeBusy",
        access_token=access_token,
        json_body={
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "items": [{"id": cid} for cid in calendar_ids],
        },
    )
    cals = data.get("calendars") or {}
    busy: list[tuple[datetime, datetime]] = []
    for cid, body in cals.items():
        for b in (body.get("busy") or []):
            try:
                s = datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
                e = datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
                busy.append((s, e))
            except Exception:
                continue
    return busy


async def create_event(
    access_token: str, calendar_id: str, event: dict,
) -> dict:
    """Create a single calendar event. `event` follows Google's
    Calendar API event shape: summary, description, start.dateTime,
    end.dateTime, attendees, etc."""
    return await _cal_request(
        "POST", f"/calendars/{urllib.parse.quote(calendar_id, safe='')}/events",
        access_token=access_token,
        params={"sendUpdates": "all"},  # send invite to attendees
        json_body=event,
    )
