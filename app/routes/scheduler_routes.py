"""
Native scheduler — config + preview + public booking endpoints.

Three audiences:

  - **Host (signed-in user)**: GET/PATCH /api/me/scheduling/config
    to set availability rules; GET /api/me/scheduling/preview to
    eyeball generated slots over the next N days.

  - **Prospect (anonymous)**: GET /api/book/{slug}/info for host
    metadata + slots; POST /api/book/{slug}/confirm to actually
    book. Public booking page itself rendered at /book/{slug}
    (HTMLResponse — no SPA routing needed for a single static page).

  - **CRM linkage**: bookings include best-effort match against
    Contact/Company by email so a successful booking lands as an
    Activity on the right company timeline (mirrors the iClosed
    webhook flow).
"""
from __future__ import annotations
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import Activity, Booking, Company, Contact, SchedulingConfig, User
from app.services.google_oauth import (
    GoogleAPIError, create_event, refresh_access_token,
)
from app.services.scheduler import (
    DEFAULT_RULES, Slot, db_busy_ranges, fetch_user_busy, generate_slots,
)

log = logging.getLogger("bmp.scheduler_routes")


# ============================================================
# Host-side config
# ============================================================

host_router = APIRouter(prefix="/api/me/scheduling", tags=["scheduler"])


async def _get_or_create_config(db: AsyncSession, user_id: int) -> SchedulingConfig:
    row = (await db.execute(
        select(SchedulingConfig).where(SchedulingConfig.user_id == user_id)
    )).scalar_one_or_none()
    if row:
        return row
    row = SchedulingConfig(user_id=user_id, rules_json=json.dumps(DEFAULT_RULES))
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def _config_payload(c: SchedulingConfig) -> dict:
    return {
        "slot_minutes": c.slot_minutes,
        "buffer_before_minutes": c.buffer_before_minutes,
        "buffer_after_minutes": c.buffer_after_minutes,
        "min_lead_time_hours": c.min_lead_time_hours,
        "max_advance_days": c.max_advance_days,
        "daily_limit": c.daily_limit,
        "rules": json.loads(c.rules_json) if c.rules_json else DEFAULT_RULES,
        "meeting_title": c.meeting_title,
        "meeting_description": c.meeting_description,
        "page_headline": c.page_headline,
        "page_intro": c.page_intro,
        "is_active": c.is_active,
    }


@host_router.get("/config")
async def get_my_scheduling_config(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    c = await _get_or_create_config(db, user.id)
    return _config_payload(c)


class SchedulingConfigPatch(BaseModel):
    slot_minutes: Optional[int] = None
    buffer_before_minutes: Optional[int] = None
    buffer_after_minutes: Optional[int] = None
    min_lead_time_hours: Optional[int] = None
    max_advance_days: Optional[int] = None
    daily_limit: Optional[int] = None
    rules: Optional[list] = None
    meeting_title: Optional[str] = None
    meeting_description: Optional[str] = None
    page_headline: Optional[str] = None
    page_intro: Optional[str] = None
    is_active: Optional[bool] = None


def _validate_rules(rules: list) -> list:
    """Normalize + sanity-check incoming rules. Drops malformed entries
    silently (we'd rather lose a bad rule than reject the whole save)."""
    out = []
    for r in rules or []:
        try:
            wd = int(r.get("weekday"))
            if wd < 0 or wd > 6:
                continue
            s = str(r["start_time"])
            e = str(r["end_time"])
            if not (re.match(r"^\d{1,2}:\d{2}$", s) and re.match(r"^\d{1,2}:\d{2}$", e)):
                continue
            if s >= e:
                continue
            out.append({"weekday": wd, "start_time": s, "end_time": e})
        except Exception:
            continue
    return out


@host_router.patch("/config")
async def patch_my_scheduling_config(
    body: SchedulingConfigPatch,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    c = await _get_or_create_config(db, user.id)
    if body.slot_minutes is not None:
        c.slot_minutes = max(5, min(180, body.slot_minutes))
    if body.buffer_before_minutes is not None:
        c.buffer_before_minutes = max(0, min(120, body.buffer_before_minutes))
    if body.buffer_after_minutes is not None:
        c.buffer_after_minutes = max(0, min(120, body.buffer_after_minutes))
    if body.min_lead_time_hours is not None:
        c.min_lead_time_hours = max(0, min(168, body.min_lead_time_hours))
    if body.max_advance_days is not None:
        c.max_advance_days = max(1, min(180, body.max_advance_days))
    if body.daily_limit is not None:
        c.daily_limit = max(0, min(50, body.daily_limit))
    if body.rules is not None:
        c.rules_json = json.dumps(_validate_rules(body.rules))
    if body.meeting_title is not None:
        c.meeting_title = body.meeting_title.strip()[:120] or "Discovery Call"
    if body.meeting_description is not None:
        c.meeting_description = body.meeting_description.strip()[:2000]
    if body.page_headline is not None:
        c.page_headline = body.page_headline.strip()[:200] or "Book a Discovery Call"
    if body.page_intro is not None:
        c.page_intro = body.page_intro.strip()[:2000]
    if body.is_active is not None:
        c.is_active = body.is_active
    await db.commit()
    await db.refresh(c)
    return _config_payload(c)


@host_router.get("/preview")
async def preview_my_slots(
    days: int = 7,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Server-side preview of the next N days of bookable slots.
    Used by the Settings UI to give the host immediate feedback when
    they change rules. Hits Google free-busy + DB."""
    c = await _get_or_create_config(db, user.id)
    days = max(1, min(c.max_advance_days, days))
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=days)
    busy, err = await fetch_user_busy(user, time_min=now, time_max=window_end)
    db_busy = await db_busy_ranges(db, user.id, time_min=now, time_max=window_end)
    slots = generate_slots(
        c, user.timezone or "America/Phoenix",
        window_start_utc=now, window_end_utc=window_end,
        busy_ranges=busy, booked_in_db=db_busy, now_utc=now,
    )
    return {
        "google_error": err,
        "host_timezone": user.timezone,
        "days": days,
        "slots": [s.to_payload(user.timezone or "America/Phoenix") for s in slots[:200]],
    }


# ============================================================
# Prospect-side public booking
# ============================================================

public_router = APIRouter(prefix="/api/book", tags=["public-booking"])


async def _resolve_host_by_slug(db: AsyncSession, slug: str) -> User:
    user = (await db.execute(
        select(User).where(User.booking_slug == slug, User.is_active == True)
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Booking page not found")
    return user


@public_router.get("/{slug}/info")
async def get_booking_info(
    slug: str, days: int = 14, viewer_tz: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Public — what to render on the booking page. No auth."""
    user = await _resolve_host_by_slug(db, slug)
    if not user.google_refresh_token:
        raise HTTPException(status_code=503, detail="Host has not connected their calendar yet")
    c = await _get_or_create_config(db, user.id)
    if not c.is_active:
        raise HTTPException(status_code=503, detail="Booking page is paused")
    days = max(1, min(c.max_advance_days, days))
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=days)
    busy, err = await fetch_user_busy(user, time_min=now, time_max=window_end)
    db_busy = await db_busy_ranges(db, user.id, time_min=now, time_max=window_end)
    if err:
        # Fail-closed when calendar isn't reachable — better to show
        # "calendar temporarily unavailable" than to overbook.
        return {
            "slug": slug,
            "host_name": user.full_name,
            "host_first_name": user.first_name,
            "page_headline": c.page_headline,
            "page_intro": c.page_intro,
            "meeting_title": c.meeting_title,
            "slot_minutes": c.slot_minutes,
            "host_timezone": user.timezone,
            "viewer_timezone": viewer_tz,
            "slots": [],
            "calendar_error": err,
        }
    viewer_tz = viewer_tz or user.timezone or "America/Phoenix"
    slots = generate_slots(
        c, user.timezone or "America/Phoenix",
        window_start_utc=now, window_end_utc=window_end,
        busy_ranges=busy, booked_in_db=db_busy, now_utc=now,
    )
    return {
        "slug": slug,
        "host_name": user.full_name,
        "host_first_name": user.first_name,
        "page_headline": c.page_headline,
        "page_intro": c.page_intro,
        "meeting_title": c.meeting_title,
        "slot_minutes": c.slot_minutes,
        "host_timezone": user.timezone,
        "viewer_timezone": viewer_tz,
        "slots": [s.to_payload(viewer_tz) for s in slots[:200]],
        "calendar_error": None,
    }


class BookingConfirmRequest(BaseModel):
    starts_at_utc: str
    name: str
    email: str
    phone: Optional[str] = None
    message: Optional[str] = None
    viewer_timezone: Optional[str] = None


def _parse_iso_utc(s: str) -> datetime:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid starts_at_utc")


async def _find_company_contact_by_email(db: AsyncSession, email: str):
    """Best-effort CRM linkage. Returns (company_id, contact_id) where
    each may be None if no match. Email-exact match wins; otherwise
    falls back to Company.domain match so we still attribute even when
    the booker uses a personal address like @gmail.com."""
    contact = (await db.execute(
        select(Contact).where(Contact.email.ilike(email))
    )).scalars().first()
    if contact:
        return contact.company_id, contact.id
    domain = (email.split("@")[-1] if "@" in email else "").lower().strip()
    if domain:
        comp = (await db.execute(
            select(Company).where(Company.domain == domain)
        )).scalar_one_or_none()
        if comp:
            return comp.id, None
    return None, None


@public_router.post("/{slug}/confirm")
async def confirm_booking(
    slug: str,
    body: BookingConfirmRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Create the Google Calendar event + persist the Booking. Returns
    the confirmation page payload (which the frontend renders)."""
    user = await _resolve_host_by_slug(db, slug)
    if not user.google_refresh_token:
        raise HTTPException(status_code=503, detail="Host has not connected their calendar yet")
    c = await _get_or_create_config(db, user.id)
    if not c.is_active:
        raise HTTPException(status_code=503, detail="Booking page is paused")

    # Server-side validation: regenerate slots for this window and
    # confirm the requested time is in the available list. This is the
    # *only* place we trust about availability — never trust the
    # frontend to have shown a real slot.
    starts_at = _parse_iso_utc(body.starts_at_utc)
    ends_at = starts_at + timedelta(minutes=c.slot_minutes)
    now = datetime.now(timezone.utc)
    if starts_at < now:
        raise HTTPException(status_code=400, detail="Slot is in the past")
    if (starts_at - now).days > c.max_advance_days:
        raise HTTPException(status_code=400, detail="Slot is past the booking window")

    busy, err = await fetch_user_busy(
        user, time_min=now, time_max=starts_at + timedelta(hours=2),
    )
    if err:
        raise HTTPException(status_code=503, detail="Calendar temporarily unavailable. Please try again.")
    db_busy = await db_busy_ranges(db, user.id, time_min=now, time_max=starts_at + timedelta(hours=2))
    slots = generate_slots(
        c, user.timezone or "America/Phoenix",
        window_start_utc=now,
        window_end_utc=starts_at + timedelta(hours=2),
        busy_ranges=busy, booked_in_db=db_busy, now_utc=now,
    )
    if not any(s.starts_at == starts_at for s in slots):
        raise HTTPException(
            status_code=409,
            detail="That time was just taken or is no longer available. Please pick another.",
        )

    # CRM linkage best-effort
    company_id, contact_id = await _find_company_contact_by_email(db, body.email.strip().lower())

    # Create Google event
    try:
        tokens = await refresh_access_token(user.google_refresh_token)
    except GoogleAPIError:
        raise HTTPException(status_code=503, detail="Could not refresh host calendar token. Please try again.")

    event = {
        "summary": f"{c.meeting_title} — {body.name}",
        "description": (
            (c.meeting_description or "").strip() + "\n\n"
            f"Booked through Backyard Marketing Pros' scheduler.\n"
            f"Prospect: {body.name} <{body.email}>"
            + (f" · {body.phone}" if body.phone else "")
            + (f"\n\nNote from prospect: {body.message}" if body.message else "")
        ),
        "start": {"dateTime": starts_at.isoformat(), "timeZone": "UTC"},
        "end":   {"dateTime": ends_at.isoformat(),   "timeZone": "UTC"},
        "attendees": [
            {"email": body.email, "displayName": body.name, "responseStatus": "needsAction"},
            {"email": user.google_email or user.email, "displayName": user.full_name,
             "responseStatus": "accepted", "organizer": True},
        ],
        "reminders": {"useDefault": True},
    }
    cal_id = user.google_calendar_id or "primary"
    try:
        gevent = await create_event(tokens.access_token, cal_id, event)
    except GoogleAPIError as e:
        log.exception(f"Failed to create Google event for booking: {e}")
        raise HTTPException(status_code=502, detail="Could not create the calendar event. Please try again.")

    booking = Booking(
        host_user_id=user.id,
        starts_at=starts_at,
        ends_at=ends_at,
        prospect_name=body.name.strip()[:160],
        prospect_email=body.email.strip().lower()[:255],
        prospect_phone=(body.phone or "").strip()[:40] or None,
        prospect_message=(body.message or "").strip()[:2000] or None,
        company_id=company_id,
        contact_id=contact_id,
        google_event_id=gevent.get("id"),
        google_event_link=gevent.get("htmlLink"),
        status="confirmed",
    )
    db.add(booking)

    # Activity entry on the matched company timeline
    if company_id:
        db.add(Activity(
            company_id=company_id,
            contact_id=contact_id,
            activity_type="meeting_booked",
            content=(
                f"Booked {c.meeting_title} for "
                f"{starts_at.astimezone(timezone.utc).isoformat()} "
                f"via /book/{slug}"
            ),
            metadata_json=json.dumps({
                "source": "native_scheduler",
                "host_user_id": user.id,
                "google_event_id": gevent.get("id"),
                "google_event_link": gevent.get("htmlLink"),
                "prospect_email": body.email,
            }),
        ))

    await db.commit()
    await db.refresh(booking)

    # Send confirmation email (Resend) in the background — never block
    # the response on email send. The Google invite already went out
    # automatically via sendUpdates=all on event creation.
    background.add_task(
        _send_booking_confirmation_email,
        booking.id, host_name=user.full_name,
    )

    return {
        "booked": True,
        "booking_id": booking.id,
        "starts_at_utc": starts_at.isoformat(),
        "ends_at_utc": ends_at.isoformat(),
        "google_event_link": gevent.get("htmlLink"),
        "host_name": user.full_name,
        "meeting_title": c.meeting_title,
    }


async def _send_booking_confirmation_email(booking_id: int, *, host_name: str) -> None:
    """Confirmation email to prospect via Resend. The Google invite
    already landed in their inbox; this is a branded BMP confirmation.
    Best-effort — silent failure is fine, the booking itself is locked
    in by the Google event + DB row."""
    import httpx
    from app.database import async_session
    if not settings.resend_api_key:
        return
    try:
        async with async_session() as db:
            b = (await db.execute(
                select(Booking).where(Booking.id == booking_id)
            )).scalar_one_or_none()
            if not b:
                return
            host = (await db.execute(select(User).where(User.id == b.host_user_id))).scalar_one_or_none()
            if not host:
                return
            local_part = (host.first_name or "bookings").lower()
            display_name = f"{host.first_name or 'BMP Bookings'} from BMP"
            from_addr = f"{display_name} <{local_part}@{settings.send_domain}>"
            subject = (
                f"Confirmed: "
                f"{b.starts_at.astimezone(timezone.utc).strftime('%a %b %-d at %-I:%M %p UTC')} "
                f"with {host_name}"
            )
            link = b.google_event_link or ""
            first = b.prospect_name.split()[0] if b.prospect_name else ""
            when = b.starts_at.astimezone(timezone.utc).strftime('%A, %B %-d at %-I:%M %p UTC')
            html = (
                f"<p>Hi {first},</p>"
                f"<p>You're booked with {host_name} on <strong>{when}</strong>. "
                "A Google Calendar invite is already in your inbox.</p>"
                + (f'<p><a href="{link}">View the meeting on your calendar</a></p>' if link else "")
                + f"<p>Talk soon,<br>{host_name}</p>"
            )
            payload = {
                "from": from_addr,
                "to": [b.prospect_email],
                "reply_to": host.google_email or host.email,
                "subject": subject,
                "html": html,
                "headers": {
                    "X-Entity-Ref-ID": f"booking-confirm-{booking_id}",
                },
                "tags": [
                    {"name": "kind", "value": "booking_confirmation"},
                    {"name": "host_user_id", "value": str(host.id)},
                ],
            }
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {settings.resend_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if r.status_code not in (200, 201):
                log.warning(f"Resend rejected booking confirm {booking_id}: {r.status_code} — {r.text[:200]}")
    except Exception as e:
        log.warning(f"Confirmation email send failed for booking {booking_id}: {e}")


# ============================================================
# Public booking page (HTML)
# ============================================================

booking_page_router = APIRouter(tags=["public-booking-page"])


@booking_page_router.get("/book/{slug}", response_class=HTMLResponse)
async def render_booking_page(slug: str, db: AsyncSession = Depends(get_db)):
    """Static brand-styled booking page. Single HTML doc with vanilla
    JS that calls /api/book/{slug}/info on load + /confirm on submit.
    No SPA routing — keeping booking flow simple & SEO-indexable."""
    user = (await db.execute(
        select(User).where(User.booking_slug == slug, User.is_active == True)
    )).scalar_one_or_none()
    if not user:
        return HTMLResponse(
            "<html><body style='font-family:system-ui;text-align:center;padding:60px'>"
            "<h2>Booking page not found</h2></body></html>",
            status_code=404,
        )
    if not user.google_refresh_token:
        return HTMLResponse(
            "<html><body style='font-family:system-ui;text-align:center;padding:60px'>"
            "<h2>This page isn't ready yet</h2>"
            "<p>The host hasn't connected their calendar.</p></body></html>",
            status_code=503,
        )
    return HTMLResponse(_render_booking_html(slug))


def _render_booking_html(slug: str) -> str:
    """The booking page template. Vanilla JS — no React/build step.
    Loads slots via /api/book/{slug}/info, groups by day in the
    visitor's local TZ, lets them pick a time, fills in name/email/
    phone, then POSTs to /confirm. Designed to be brand-agnostic
    enough that swapping logo + colors makes it tenant-skinnable in
    the SaaS world."""
    return r"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Book a Discovery Call — BMP</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background:#fafafa; color:#222; margin:0; padding:20px; }
  .wrap { max-width:780px; margin:30px auto; background:white; border-radius:14px; box-shadow:0 4px 20px rgba(0,0,0,0.06); overflow:hidden; }
  header { background:linear-gradient(135deg,#FFF8F0,#fff); padding:28px 32px; border-bottom:1px solid #f1ebe5; }
  header h1 { margin:0; font-size:24px; color:#1a1a1a; }
  header .intro { color:#666; font-size:14px; margin-top:6px; line-height:1.55; }
  .body { padding:24px 32px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:18px; min-height:360px; }
  @media (max-width:680px){ .grid { grid-template-columns:1fr; } }
  .day-list { border-right:1px solid #f1f1f1; padding-right:14px; max-height:540px; overflow-y:auto; }
  @media (max-width:680px){ .day-list { border-right:0; padding-right:0; } }
  .day { margin-bottom:12px; }
  .day h4 { margin:0 0 6px; font-size:13px; color:#666; text-transform:uppercase; letter-spacing:0.5px; }
  .slot-btn { display:block; width:100%; text-align:left; padding:8px 12px; margin:4px 0; background:white; border:1px solid #e6e6e6; border-radius:6px; cursor:pointer; font-size:13px; color:#333; transition:all 0.15s; }
  .slot-btn:hover { border-color:#E65100; color:#E65100; }
  .slot-btn.selected { background:#E65100; color:white; border-color:#E65100; }
  .form-pane { padding-left:14px; }
  @media (max-width:680px){ .form-pane { padding-left:0; } }
  .form-pane h3 { margin:0 0 10px; font-size:15px; }
  .form-pane label { display:block; font-size:12px; color:#666; margin-top:10px; }
  .form-pane input, .form-pane textarea { width:100%; padding:9px 11px; border:1px solid #ddd; border-radius:6px; font-size:14px; box-sizing:border-box; font-family:inherit; }
  .form-pane button { background:#E65100; color:white; border:0; padding:11px 18px; border-radius:6px; font-size:14px; cursor:pointer; margin-top:14px; font-weight:600; }
  .form-pane button:disabled { background:#ccc; cursor:not-allowed; }
  .selected-summary { padding:10px 12px; background:#FFF8F0; border-radius:6px; font-size:13px; margin-bottom:8px; color:#333; }
  .err { color:#c0392b; font-size:13px; margin-top:10px; }
  .empty { color:#888; font-size:13px; padding:20px; text-align:center; }
  .booked { text-align:center; padding:40px 20px; }
  .booked h2 { color:#1b5e20; }
</style>
</head>
<body>
  <div class="wrap" id="root">
    <header>
      <h1 id="page-headline">Loading…</h1>
      <div class="intro" id="page-intro"></div>
      <div style="font-size:11px;color:#888;margin-top:8px"><span id="meeting-meta"></span></div>
    </header>
    <div class="body" id="body">
      <div class="empty">Loading available times…</div>
    </div>
  </div>

<script>
const SLUG = """ + json.dumps(slug) + r""";
const VIEWER_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
let info = null;
let selectedSlot = null;

function fmtDay(iso) {
  return new Date(iso).toLocaleDateString(undefined, { weekday:'long', month:'short', day:'numeric' });
}
function fmtTime(iso) {
  return new Date(iso).toLocaleTimeString(undefined, { hour:'numeric', minute:'2-digit' });
}

async function load() {
  const r = await fetch(`/api/book/${SLUG}/info?days=14&viewer_tz=${encodeURIComponent(VIEWER_TZ)}`);
  if (!r.ok) {
    document.getElementById('body').innerHTML = `<div class="empty"><strong>Couldn't load this page.</strong><br>${(await r.json()).detail || ''}</div>`;
    return;
  }
  info = await r.json();
  document.getElementById('page-headline').textContent = info.page_headline;
  document.getElementById('page-intro').textContent = info.page_intro;
  document.getElementById('meeting-meta').textContent = `${info.meeting_title} · ${info.slot_minutes} min · with ${info.host_name} · times shown in ${VIEWER_TZ}`;
  if (info.calendar_error) {
    document.getElementById('body').innerHTML = `<div class="empty">Calendar temporarily unavailable. Please try again in a few minutes.</div>`;
    return;
  }
  if (!info.slots || !info.slots.length) {
    document.getElementById('body').innerHTML = `<div class="empty">No available times in the next two weeks. Please check back later.</div>`;
    return;
  }
  // Group by day
  const byDay = {};
  for (const s of info.slots) {
    const dayKey = s.starts_at_local.slice(0, 10);
    if (!byDay[dayKey]) byDay[dayKey] = [];
    byDay[dayKey].push(s);
  }
  const days = Object.keys(byDay).sort();
  let html = '<div class="grid"><div class="day-list">';
  for (const d of days) {
    html += `<div class="day"><h4>${fmtDay(d)}</h4>`;
    for (const s of byDay[d]) {
      html += `<button class="slot-btn" data-utc="${encodeURIComponent(s.starts_at_utc)}" data-local="${encodeURIComponent(s.starts_at_local)}">${fmtTime(s.starts_at_local)}</button>`;
    }
    html += '</div>';
  }
  html += `</div><div class="form-pane">
    <h3>Your details</h3>
    <div id="form-empty" style="color:#888;font-size:13px">Pick a time to book.</div>
    <div id="form-fields" style="display:none">
      <div class="selected-summary" id="selected-summary"></div>
      <label>Full name *</label><input id="bk-name" required>
      <label>Email *</label><input id="bk-email" type="email" required>
      <label>Phone (optional)</label><input id="bk-phone" type="tel">
      <label>What would you like to discuss? (optional)</label><textarea id="bk-msg" rows="3"></textarea>
      <button id="bk-submit" onclick="confirmBooking()">Confirm booking</button>
      <div class="err" id="bk-err"></div>
    </div>
  </div></div>`;
  document.getElementById('body').innerHTML = html;
  document.querySelectorAll('.slot-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.slot-btn').forEach(b => b.classList.remove('selected'));
      btn.classList.add('selected');
      selectedSlot = {
        utc: decodeURIComponent(btn.dataset.utc),
        local: decodeURIComponent(btn.dataset.local),
      };
      document.getElementById('form-empty').style.display = 'none';
      document.getElementById('form-fields').style.display = '';
      document.getElementById('selected-summary').innerHTML = `<strong>Selected:</strong> ${fmtDay(selectedSlot.local)} at ${fmtTime(selectedSlot.local)}`;
    });
  });
}

async function confirmBooking() {
  const name = document.getElementById('bk-name').value.trim();
  const email = document.getElementById('bk-email').value.trim();
  const phone = document.getElementById('bk-phone').value.trim();
  const message = document.getElementById('bk-msg').value.trim();
  const errEl = document.getElementById('bk-err');
  errEl.textContent = '';
  if (!name || !email) { errEl.textContent = 'Name and email are required.'; return; }
  if (!selectedSlot) { errEl.textContent = 'Please pick a time first.'; return; }
  const btn = document.getElementById('bk-submit');
  btn.disabled = true;
  btn.textContent = 'Booking…';
  const r = await fetch(`/api/book/${SLUG}/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      starts_at_utc: selectedSlot.utc,
      name, email, phone, message,
      viewer_timezone: VIEWER_TZ,
    }),
  });
  if (!r.ok) {
    const data = await r.json().catch(() => ({}));
    errEl.textContent = data.detail || 'Could not complete the booking. Please try again.';
    btn.disabled = false;
    btn.textContent = 'Confirm booking';
    return;
  }
  const data = await r.json();
  document.getElementById('root').innerHTML = `
    <div class="booked">
      <h2>You're booked!</h2>
      <p><strong>${fmtDay(selectedSlot.local)} at ${fmtTime(selectedSlot.local)}</strong></p>
      <p>A calendar invite is on its way to <strong>${email}</strong>.</p>
      ${data.google_event_link ? `<p><a href="${data.google_event_link}">View on your calendar</a></p>` : ''}
      <p style="color:#888;font-size:13px">Talk soon,<br>${data.host_name}</p>
    </div>
  `;
}

load();
</script>
</body></html>
"""
