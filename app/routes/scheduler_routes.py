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


_VALID_QUESTION_TYPES = {"short_text", "long_text", "url", "single_select"}
_DEFAULT_BOOKING_QUESTIONS = []  # Empty by default — name/email/phone are always shown.

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_DEFAULT_BRAND = "#E65100"
_DEFAULT_ACCENT_BG = "#FFF8F0"


def _normalize_hex(s: Optional[str], fallback: str) -> str:
    """Validate + normalize a hex string. Bad input → fallback. We
    intentionally don't accept #RGB short-form to keep the surface
    narrow — color picker UIs always emit #RRGGBB."""
    if not s:
        return fallback
    s = s.strip()
    if _HEX_COLOR_RE.match(s):
        return s.lower() if s.startswith("#") else f"#{s.lower()}"
    return fallback


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
        "meeting_type": c.meeting_type or "google_meet",
        "meeting_location_details": c.meeting_location_details or "",
        "questions": json.loads(c.booking_questions_json) if c.booking_questions_json else _DEFAULT_BOOKING_QUESTIONS,
        "brand_color": _normalize_hex(c.brand_color, _DEFAULT_BRAND),
        "accent_bg_color": _normalize_hex(c.accent_bg_color, _DEFAULT_ACCENT_BG),
        "logo_url": c.logo_url or "",
        "conflict_calendar_ids": json.loads(c.conflict_calendar_ids_json) if c.conflict_calendar_ids_json else [],
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
    meeting_type: Optional[str] = None
    meeting_location_details: Optional[str] = None
    questions: Optional[list] = None
    brand_color: Optional[str] = None
    accent_bg_color: Optional[str] = None
    logo_url: Optional[str] = None
    conflict_calendar_ids: Optional[list[str]] = None


def _validate_questions(raw: list) -> list:
    """Sanity-check incoming custom questions. Each entry must have a
    type from _VALID_QUESTION_TYPES and a non-empty label. Drops
    malformed entries; reassigns positions sequentially. Auto-generates
    a stable `key` (slug from label) when the client didn't supply one,
    so answers can persist with a meaningful column name across edits."""
    out: list[dict] = []
    seen_keys: set[str] = set()
    for i, q in enumerate(raw or []):
        if not isinstance(q, dict):
            continue
        qtype = (q.get("type") or "").strip()
        if qtype not in _VALID_QUESTION_TYPES:
            continue
        label = (q.get("label") or "").strip()
        if not label:
            continue
        key = (q.get("key") or "").strip().lower()
        if not key:
            key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or f"q{i+1}"
        # Avoid collisions with built-in fields + earlier custom keys
        builtin = {"name", "email", "phone", "message"}
        base = key
        n = 2
        while key in builtin or key in seen_keys:
            key = f"{base}_{n}"
            n += 1
        seen_keys.add(key)
        entry = {
            "id": q.get("id") or f"q{i+1}",
            "key": key,
            "label": label[:200],
            "type": qtype,
            "required": bool(q.get("required")),
            "position": i,
        }
        if qtype == "single_select":
            opts = q.get("options") or []
            entry["options"] = [str(o).strip()[:120] for o in opts if str(o).strip()][:20]
            if not entry["options"]:
                continue  # single_select with no options is useless
        out.append(entry)
    out.sort(key=lambda x: x["position"])
    for i, e in enumerate(out):
        e["position"] = i
    return out


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
    if body.meeting_type is not None:
        mt = body.meeting_type.strip().lower()
        if mt not in ("google_meet", "phone", "in_person", "custom_link"):
            mt = "google_meet"
        c.meeting_type = mt
    if body.meeting_location_details is not None:
        c.meeting_location_details = body.meeting_location_details.strip()[:1000] or None
    if body.questions is not None:
        c.booking_questions_json = json.dumps(_validate_questions(body.questions))
    if body.brand_color is not None:
        c.brand_color = _normalize_hex(body.brand_color, _DEFAULT_BRAND)
    if body.accent_bg_color is not None:
        c.accent_bg_color = _normalize_hex(body.accent_bg_color, _DEFAULT_ACCENT_BG)
    if body.logo_url is not None:
        url = body.logo_url.strip()[:500]
        # Reject non-https logo URLs — embedding http:// images in our
        # https booking page triggers mixed-content blocks in browsers.
        if url and not url.startswith("https://"):
            raise HTTPException(status_code=400, detail="Logo URL must start with https://")
        c.logo_url = url or None
    if body.conflict_calendar_ids is not None:
        # Drop empties + dedupe + cap at 10 (sanity)
        clean: list[str] = []
        for cid in body.conflict_calendar_ids:
            cid = (cid or "").strip()
            if cid and cid not in clean:
                clean.append(cid)
        clean = clean[:10]
        c.conflict_calendar_ids_json = json.dumps(clean) if clean else None
    await db.commit()
    await db.refresh(c)
    return _config_payload(c)


@host_router.get("/upcoming")
async def upcoming_bookings(
    days: int = 30,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the rep's confirmed bookings starting in the next N
    days. Includes prospect info, matched company/contact, Google
    event link, and (when applicable) the Meet link. Used by the
    Calendar page's Upcoming tab + future morning-brief / dashboard
    widgets."""
    days = max(1, min(180, int(days or 30)))
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days)
    rows = (await db.execute(
        select(Booking).where(
            Booking.host_user_id == user.id,
            Booking.status == "confirmed",
            Booking.starts_at >= now - timedelta(hours=2),  # include in-progress
            Booking.starts_at < end,
        ).order_by(Booking.starts_at.asc())
    )).scalars().all()
    # Pull company names in one go for context
    company_ids = {b.company_id for b in rows if b.company_id}
    companies = {}
    if company_ids:
        cm = (await db.execute(
            select(Company).where(Company.id.in_(company_ids))
        )).scalars().all()
        companies = {c.id: c for c in cm}
    payload = []
    for b in rows:
        s = b.starts_at if b.starts_at.tzinfo else b.starts_at.replace(tzinfo=timezone.utc)
        e = b.ends_at if b.ends_at.tzinfo else b.ends_at.replace(tzinfo=timezone.utc)
        comp = companies.get(b.company_id) if b.company_id else None
        payload.append({
            "id": b.id,
            "starts_at_utc": s.isoformat(),
            "ends_at_utc": e.isoformat(),
            "duration_minutes": int((e - s).total_seconds() // 60),
            "prospect_name": b.prospect_name,
            "prospect_email": b.prospect_email,
            "prospect_phone": b.prospect_phone,
            "company_id": b.company_id,
            "company_name": comp.name if comp else None,
            "contact_id": b.contact_id,
            "google_event_link": b.google_event_link,
            "google_meet_link": b.google_meet_link,
            "prospect_timezone": getattr(b, "prospect_timezone", None),
        })
    return {
        "host_timezone": user.timezone or "America/Phoenix",
        "count": len(payload),
        "bookings": payload,
    }


@host_router.get("/my-google-calendars")
async def list_my_google_calendars(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the user's Google calendars — used by the Calendar Settings
    UI to populate the "conflict calendars" multi-select. Returns id +
    summary + primary flag. Empty list if Calendar isn't connected."""
    if not user.google_refresh_token:
        return {"connected": False, "calendars": []}
    try:
        tokens = await refresh_access_token(user.google_refresh_token)
    except GoogleAPIError as e:
        log.warning(f"my-google-calendars refresh failed for user {user.id}: {e}")
        return {"connected": False, "calendars": [], "error": "google_refresh_failed"}
    try:
        from app.services.google_oauth import list_calendars
        cals = await list_calendars(tokens.access_token)
    except GoogleAPIError as e:
        log.warning(f"my-google-calendars list failed for user {user.id}: {e}")
        return {"connected": True, "calendars": [], "error": "google_list_failed"}
    out = []
    for c in cals:
        cid = c.get("id")
        if not cid:
            continue
        out.append({
            "id": cid,
            "summary": c.get("summary") or cid,
            "primary": bool(c.get("primary")),
            "is_write_target": cid == user.google_calendar_id,
            "access_role": c.get("accessRole"),
        })
    return {"connected": True, "calendars": out}


@host_router.get("/preview")
async def preview_my_slots(
    days: int = 7,
    effective: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Server-side preview of the next N days of bookable slots.

    Two modes:
      - effective=false (default) — preview the user's OWN calendar.
        Used by Settings → Calendar so the host sees what *their*
        public booking page looks like.
      - effective=true — preview the calendar this user *books on*
        (their own, or the configured default_booking_host). Used by
        the in-app "Schedule a meeting" modal so a BDR routed to the
        admin sees the admin's slots.
    """
    if effective:
        from app.services.booking_host import resolve_booking_host
        target = await resolve_booking_host(db, user)
    else:
        target = user
    c = await _get_or_create_config(db, target.id)
    days = max(1, min(c.max_advance_days, days))
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=days)
    busy, err = await fetch_user_busy(target, time_min=now, time_max=window_end, config=c)
    db_busy = await db_busy_ranges(db, target.id, time_min=now, time_max=window_end)
    slots = generate_slots(
        c, target.timezone or "America/Phoenix",
        window_start_utc=now, window_end_utc=window_end,
        busy_ranges=busy, booked_in_db=db_busy, now_utc=now,
    )
    return {
        "google_error": err,
        "host_timezone": target.timezone,
        "host_user_id": target.id,
        "host_name": target.full_name,
        "is_routed": target.id != user.id,
        "days": days,
        "slots": [s.to_payload(target.timezone or "America/Phoenix") for s in slots[:200]],
    }


# ============================================================
# Rep-initiated scheduling — book a meeting *with* a known contact
# ============================================================
#
# This is the "Schedule with Contact" flow Steve asked for: a BDR is
# looking at a contact card in the CRM and wants to drop a meeting
# straight onto their calendar without sending the prospect through
# the public booking page. We re-use the slot generator + Google
# event creator from the public flow, but:
#   - The host is the authenticated user (no slug lookup)
#   - is_active doesn't gate this — the BDR can always book on
#     their own time, even if they've paused public bookings
#   - Custom intake questions are skipped — the rep already knows
#     the contact, no need to gate on form completion
#   - We mirror the booked event into the CRM via Activity row,
#     same Activity shape as the public flow so dashboards / hot-
#     lead detectors don't need to know which path booked it


class InternalBookingRequest(BaseModel):
    contact_id: int
    starts_at_utc: str
    custom_meeting_title: Optional[str] = None
    note: Optional[str] = None  # internal note for the rep, surfaced in event description
    # When the contact has no email on file, the modal collects one
    # inline. We persist that to the contact record before booking
    # (Google requires an attendee email). Phone is optional.
    prospect_email_override: Optional[str] = None
    prospect_phone_override: Optional[str] = None


@host_router.post("/book-for-contact")
async def book_for_contact(
    body: InternalBookingRequest,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Rep-initiated booking. If the rep has a default_booking_host
    configured (e.g. BDR routed to admin's "Discovery Call" calendar),
    we book on the HOST's calendar with the host as the organizer; the
    rep stays on the Activity audit as the booker. Otherwise it's the
    rep's own calendar like before."""
    from app.services.booking_host import resolve_booking_host
    host = await resolve_booking_host(db, user)
    if not host.google_refresh_token:
        if host.id == user.id:
            raise HTTPException(status_code=400, detail="Connect Google Calendar in Calendar Settings first.")
        raise HTTPException(
            status_code=400,
            detail=f"The booking host ({host.full_name}) hasn't connected their calendar yet.",
        )

    # Resolve contact + scope: a sales_rep can only schedule with
    # contacts whose company they own. Admins / super_admins skip the
    # ownership gate.
    contact = (await db.execute(
        select(Contact).where(Contact.id == body.contact_id)
    )).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    company = (await db.execute(
        select(Company).where(Company.id == contact.company_id)
    )).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Contact has no company")
    if user.role == "sales_rep" and company.assigned_to and company.assigned_to != user.id:
        raise HTTPException(status_code=403, detail="Not your company")

    # Apply email/phone overrides from the modal if the contact had
    # missing data. Persist back so the contact record stays current.
    override_email = (body.prospect_email_override or "").strip().lower()
    override_phone = (body.prospect_phone_override or "").strip()
    if override_email and "@" in override_email:
        if not contact.email:
            contact.email = override_email
    if override_phone and not contact.phone:
        contact.phone = override_phone

    if not contact.email:
        raise HTTPException(status_code=400, detail="Contact has no email — Google requires an attendee email")

    # Validate against the HOST's calendar + scheduling config — the
    # rep is just acting on behalf of the host.
    c = await _get_or_create_config(db, host.id)
    starts_at = _parse_iso_utc(body.starts_at_utc)
    ends_at = starts_at + timedelta(minutes=c.slot_minutes)
    now = datetime.now(timezone.utc)
    if starts_at < now:
        raise HTTPException(status_code=400, detail="Slot is in the past")
    # Allow rep-initiated booking up to 90 days out regardless of the
    # public-booking max_advance_days (which is a prospect-facing UX
    # cap, not a hard rule).
    if (starts_at - now).days > max(c.max_advance_days, 90):
        raise HTTPException(status_code=400, detail="Slot is past the booking window")

    busy, err = await fetch_user_busy(
        host, time_min=now, time_max=starts_at + timedelta(hours=2), config=c,
    )
    if err:
        raise HTTPException(status_code=503, detail="Calendar temporarily unavailable. Please try again.")
    db_busy = await db_busy_ranges(db, host.id, time_min=now, time_max=starts_at + timedelta(hours=2))
    slots = generate_slots(
        c, host.timezone or "America/Phoenix",
        window_start_utc=now,
        window_end_utc=starts_at + timedelta(hours=2),
        busy_ranges=busy, booked_in_db=db_busy, now_utc=now,
    )
    if not any(s.starts_at == starts_at for s in slots):
        raise HTTPException(
            status_code=409,
            detail="That time was just taken or is no longer available. Please pick another.",
        )

    prospect_name = contact.full_name or contact.email
    prospect_email = contact.email.strip().lower()
    prospect_phone = contact.phone or ""
    meeting_title = (body.custom_meeting_title or c.meeting_title).strip() or "Meeting"

    # Build description block — host's standard meeting description
    # plus the rep's internal note, plus contact context.
    desc_parts = []
    if (c.meeting_description or "").strip():
        desc_parts.append((c.meeting_description or "").strip())
        desc_parts.append("")
    desc_parts.append(f"Booked by {user.full_name} for {prospect_name} <{prospect_email}>"
                      + (f" · {prospect_phone}" if prospect_phone else ""))
    if company.name:
        desc_parts.append(f"Company: {company.name}")
    if body.note:
        desc_parts.append(f"\nNote: {body.note}")

    # Honor host's meeting_type — same logic as the public flow.
    meeting_type = (c.meeting_type or "google_meet").lower()
    location_field: Optional[str] = None
    with_meet_link = False
    if meeting_type == "google_meet":
        with_meet_link = True
    elif meeting_type == "phone":
        location_field = (f"Phone — {prospect_phone}" if prospect_phone else "Phone call")
        if prospect_phone:
            desc_parts.append(f"\n📞 You'll call {prospect_name} at {prospect_phone}.")
    elif meeting_type == "in_person":
        location_field = (c.meeting_location_details or "").strip() or "In person"
    elif meeting_type == "custom_link":
        link = (c.meeting_location_details or "").strip()
        location_field = link or "Online meeting"
        if link and link.startswith(("http://", "https://")):
            desc_parts.append(f"\n🔗 Meeting link: {link}")

    try:
        tokens = await refresh_access_token(host.google_refresh_token)
    except GoogleAPIError:
        raise HTTPException(status_code=503, detail="Could not refresh host's Google token. Reconnect Calendar.")

    # Build attendee list: prospect + host (as organizer). When the BDR
    # is a different person from the host, also include the BDR so they
    # get the calendar invite on their own Google account.
    attendees = [
        {"email": prospect_email, "displayName": prospect_name, "responseStatus": "needsAction"},
        {"email": host.google_email or host.email, "displayName": host.full_name,
         "responseStatus": "accepted", "organizer": True},
    ]
    if user.id != host.id and (user.email or "").strip():
        attendees.append({
            "email": user.email,
            "displayName": user.full_name,
            "responseStatus": "accepted",
        })

    event = {
        "summary": f"{meeting_title} — {prospect_name}",
        "description": "\n".join(desc_parts).strip(),
        "start": {"dateTime": starts_at.isoformat(), "timeZone": "UTC"},
        "end":   {"dateTime": ends_at.isoformat(),   "timeZone": "UTC"},
        "attendees": attendees,
        "reminders": {"useDefault": True},
    }
    if location_field:
        event["location"] = location_field
    cal_id = host.google_calendar_id or "primary"
    try:
        gevent = await create_event(
            tokens.access_token, cal_id, event,
            with_meet_link=with_meet_link,
        )
    except GoogleAPIError as e:
        log.exception(f"book-for-contact create_event failed: {e}")
        raise HTTPException(status_code=502, detail="Could not create the calendar event. Try again.")

    meet_link: Optional[str] = None
    if with_meet_link:
        meet_link = gevent.get("hangoutLink")
        if not meet_link:
            for ep in ((gevent.get("conferenceData") or {}).get("entryPoints") or []):
                if ep.get("entryPointType") == "video" and ep.get("uri"):
                    meet_link = ep["uri"]
                    break

    booking = Booking(
        host_user_id=host.id,
        starts_at=starts_at,
        ends_at=ends_at,
        prospect_name=prospect_name[:160],
        prospect_email=prospect_email[:255],
        prospect_phone=prospect_phone[:40] or None,
        prospect_message=(body.note or "")[:2000] or None,
        company_id=company.id,
        contact_id=contact.id,
        google_event_id=gevent.get("id"),
        google_event_link=gevent.get("htmlLink"),
        google_meet_link=meet_link,
        status="confirmed",
    )
    db.add(booking)

    booker_suffix = "" if user.id == host.id else f" (booked by {user.full_name})"
    db.add(Activity(
        company_id=company.id,
        contact_id=contact.id,
        user_id=user.id,
        activity_type="meeting_booked",
        content=(
            f"Scheduled {meeting_title} with {prospect_name} for "
            f"{starts_at.astimezone(timezone.utc).isoformat()}{booker_suffix}"
        ),
        metadata_json=json.dumps({
            "source": "rep_initiated",
            "host_user_id": host.id,
            "booked_by_user_id": user.id,
            "google_event_id": gevent.get("id"),
            "google_event_link": gevent.get("htmlLink"),
            "google_meet_link": meet_link,
            "meeting_type": meeting_type,
            "prospect_email": prospect_email,
        }),
    ))

    # Bump the company status if it's still in early stages.
    if company.status in ("new", "pursuing", "sequencing", "contacted"):
        company.status = "qualified"

    await db.commit()
    await db.refresh(booking)

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
        "google_meet_link": meet_link,
        "meeting_type": meeting_type,
        "host_name": user.full_name,
        "meeting_title": meeting_title,
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
    busy, err = await fetch_user_busy(user, time_min=now, time_max=window_end, config=c)
    db_busy = await db_busy_ranges(db, user.id, time_min=now, time_max=window_end)
    questions = json.loads(c.booking_questions_json) if c.booking_questions_json else []
    base = {
        "slug": slug,
        "host_name": user.full_name,
        "host_first_name": user.first_name,
        "page_headline": c.page_headline,
        "page_intro": c.page_intro,
        "meeting_title": c.meeting_title,
        "slot_minutes": c.slot_minutes,
        "meeting_type": c.meeting_type or "google_meet",
        # Don't leak host's address / personal Zoom link before booking
        # — the prospect only sees it on the confirmation. The page just
        # shows a friendly label like "Google Meet" / "Phone" / etc.
        "host_timezone": user.timezone,
        "viewer_timezone": viewer_tz or user.timezone or "America/Phoenix",
        "questions": questions,
    }
    if err:
        # Fail-closed when calendar isn't reachable — better to show
        # "calendar temporarily unavailable" than to overbook.
        return {**base, "slots": [], "calendar_error": err}
    viewer_tz = base["viewer_timezone"]
    slots = generate_slots(
        c, user.timezone or "America/Phoenix",
        window_start_utc=now, window_end_utc=window_end,
        busy_ranges=busy, booked_in_db=db_busy, now_utc=now,
    )
    return {**base, "slots": [s.to_payload(viewer_tz) for s in slots[:200]], "calendar_error": None}


class BookingConfirmRequest(BaseModel):
    starts_at_utc: str
    name: str
    email: str
    phone: Optional[str] = None
    message: Optional[str] = None
    viewer_timezone: Optional[str] = None
    answers: Optional[dict] = None  # custom-question answers, keyed by question.key


def _parse_iso_utc(s: str) -> datetime:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid starts_at_utc")


def _filter_answers(host_questions: list, raw: dict) -> dict:
    """Coerce + clamp custom answers against the host's question schema.
    Drops keys we don't know about, validates single_select against the
    declared options, truncates long_text. Never raises — bad input
    just means an empty value for that key."""
    valid_keys = {q["key"]: q for q in host_questions if isinstance(q, dict) and q.get("key")}
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        q = valid_keys.get(k)
        if not q:
            continue
        sval = str(v).strip() if v is not None else ""
        if not sval:
            continue
        qtype = q.get("type")
        if qtype == "single_select":
            opts = q.get("options") or []
            if sval not in opts:
                continue
            out[k] = sval
        elif qtype == "url":
            out[k] = sval[:500]
        elif qtype == "long_text":
            out[k] = sval[:5000]
        else:  # short_text
            out[k] = sval[:300]
    return out


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
        user, time_min=now, time_max=starts_at + timedelta(hours=2), config=c,
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

    # Validate + filter custom answers against the host's question schema
    host_questions = json.loads(c.booking_questions_json) if c.booking_questions_json else []
    answers_clean = _filter_answers(host_questions, body.answers or {})
    # Required-field check
    for q in host_questions:
        if q.get("required") and not str(answers_clean.get(q["key"], "")).strip():
            raise HTTPException(status_code=400, detail=f"Required field missing: {q['label']}")

    # Build the description block including built-in fields + answers.
    # Reads top-to-bottom in the calendar invite — Google's UI shows
    # the description prominently, so it's the right place to surface
    # what the prospect told us.
    desc_parts = []
    if (c.meeting_description or "").strip():
        desc_parts.append((c.meeting_description or "").strip())
    desc_parts.append("")  # blank line
    desc_parts.append("Booked through Backyard Marketing Pros' scheduler.")
    desc_parts.append(
        f"Prospect: {body.name} <{body.email}>"
        + (f" · {body.phone}" if body.phone else "")
    )
    # Legacy free-form `message` is no longer collected on the booking
    # page (hosts use custom long_text questions instead). Older API
    # callers may still send it — surface it if present.
    if body.message:
        desc_parts.append(f"\nNote from prospect: {body.message}")
    if host_questions and answers_clean:
        desc_parts.append("\n— Intake answers —")
        for q in host_questions:
            v = answers_clean.get(q["key"])
            if v is None or str(v).strip() == "":
                continue
            desc_parts.append(f"{q['label']}: {v}")

    # Honor the host's meeting_type. google_meet → conferenceData attached
    # so Google generates a Meet link. Other types → set event.location
    # (Google Calendar's invite renders the location prominently).
    meeting_type = (c.meeting_type or "google_meet").lower()
    location_field: Optional[str] = None
    with_meet_link = False
    if meeting_type == "google_meet":
        with_meet_link = True
    elif meeting_type == "phone":
        prospect_phone = (body.phone or "").strip()
        if prospect_phone:
            location_field = f"Phone — {prospect_phone}"
            desc_parts.append(f"\n📞 Host will call you at {prospect_phone}.")
        else:
            location_field = "Phone call"
            desc_parts.append("\n📞 This is a phone call. The host will reach out.")
    elif meeting_type == "in_person":
        addr = (c.meeting_location_details or "").strip()
        location_field = addr or "In person — host will share address"
    elif meeting_type == "custom_link":
        link = (c.meeting_location_details or "").strip()
        location_field = link or "Online meeting link"
        if link and link.startswith(("http://", "https://")):
            desc_parts.append(f"\n🔗 Meeting link: {link}")

    # Create Google event
    try:
        tokens = await refresh_access_token(user.google_refresh_token)
    except GoogleAPIError:
        raise HTTPException(status_code=503, detail="Could not refresh host calendar token. Please try again.")

    event = {
        "summary": f"{c.meeting_title} — {body.name}",
        "description": "\n".join(desc_parts).strip(),
        "start": {"dateTime": starts_at.isoformat(), "timeZone": "UTC"},
        "end":   {"dateTime": ends_at.isoformat(),   "timeZone": "UTC"},
        "attendees": [
            {"email": body.email, "displayName": body.name, "responseStatus": "needsAction"},
            {"email": user.google_email or user.email, "displayName": user.full_name,
             "responseStatus": "accepted", "organizer": True},
        ],
        "reminders": {"useDefault": True},
    }
    if location_field:
        event["location"] = location_field
    cal_id = user.google_calendar_id or "primary"
    try:
        gevent = await create_event(
            tokens.access_token, cal_id, event,
            with_meet_link=with_meet_link,
        )
    except GoogleAPIError as e:
        log.exception(f"Failed to create Google event for booking: {e}")
        raise HTTPException(status_code=502, detail="Could not create the calendar event. Please try again.")

    # Pull the Meet link Google just generated (if we asked for one).
    # `hangoutLink` is the canonical field; conferenceData.entryPoints
    # is the fallback for older API responses.
    meet_link: Optional[str] = None
    if with_meet_link:
        meet_link = gevent.get("hangoutLink")
        if not meet_link:
            entries = ((gevent.get("conferenceData") or {}).get("entryPoints") or [])
            for e in entries:
                if (e.get("entryPointType") == "video") and e.get("uri"):
                    meet_link = e["uri"]
                    break

    prospect_tz = (body.viewer_timezone or "").strip() or None
    booking = Booking(
        host_user_id=user.id,
        starts_at=starts_at,
        ends_at=ends_at,
        prospect_name=body.name.strip()[:160],
        prospect_email=body.email.strip().lower()[:255],
        prospect_phone=(body.phone or "").strip()[:40] or None,
        prospect_message=(body.message or "").strip()[:2000] or None,
        prospect_timezone=prospect_tz,
        answers_json=json.dumps(answers_clean) if answers_clean else None,
        company_id=company_id,
        contact_id=contact_id,
        google_event_id=gevent.get("id"),
        google_event_link=gevent.get("htmlLink"),
        google_meet_link=meet_link,
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
                "google_meet_link": meet_link,
                "meeting_type": meeting_type,
                "prospect_email": body.email,
                "answers": answers_clean or None,
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
        "google_meet_link": meet_link,
        "meeting_type": meeting_type,
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
            from zoneinfo import ZoneInfo
            # Format time in the prospect's timezone (or UTC fallback)
            p_tz_name = b.prospect_timezone or "UTC"
            try:
                p_tz = ZoneInfo(p_tz_name)
            except Exception:
                p_tz = timezone.utc
                p_tz_name = "UTC"
            prospect_local = b.starts_at.replace(tzinfo=timezone.utc).astimezone(p_tz)
            # Format time in the host's timezone
            h_tz_name = host.timezone or "America/Phoenix"
            try:
                h_tz = ZoneInfo(h_tz_name)
            except Exception:
                h_tz = timezone.utc
                h_tz_name = "UTC"
            host_local = b.starts_at.replace(tzinfo=timezone.utc).astimezone(h_tz)

            tz_short = {"America/Phoenix": "MST", "America/Los_Angeles": "PT", "America/Denver": "MT",
                        "America/Chicago": "CT", "America/New_York": "ET", "Etc/UTC": "UTC"}
            p_tz_label = tz_short.get(p_tz_name, p_tz_name.split("/")[-1])
            h_tz_label = tz_short.get(h_tz_name, h_tz_name.split("/")[-1])

            when_prospect = prospect_local.strftime('%A, %B %-d at %-I:%M %p') + f" {p_tz_label}"
            when_host = host_local.strftime('%-I:%M %p') + f" {h_tz_label}"

            subject = f"Confirmed: {prospect_local.strftime('%a %b %-d at %-I:%M %p')} {p_tz_label} with {host_name}"

            link = b.google_event_link or ""
            first = b.prospect_name.split()[0] if b.prospect_name else ""
            # Show prospect's time prominently, host's time in parentheses if different
            if p_tz_name != h_tz_name:
                time_line = f"<strong>{when_prospect}</strong> ({when_host} {host_name}'s time)"
            else:
                time_line = f"<strong>{when_prospect}</strong>"
            html = (
                f"<p>Hi {first},</p>"
                f"<p>You're booked with {host_name} on {time_line}. "
                "A Google Calendar invite is already in your inbox.</p>"
                + (f'<p><a href="{link}">View the meeting on your calendar</a></p>' if link else "")
                + f"<p>Talk soon,<br>{host_name}</p>"
            )
            from app.services.html_to_text import html_to_plain_text
            payload = {
                "from": from_addr,
                "to": [b.prospect_email],
                "reply_to": host.google_email or host.email,
                "subject": subject,
                "html": html,
                "text": html_to_plain_text(html),
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
    No SPA routing — keeping booking flow simple & SEO-indexable.

    Cache-Control: no-store so a fresh deploy reaches prospects
    immediately. The dynamic data (slots, questions) loads via the
    JSON `/info` endpoint regardless of HTML cache, but the embedded
    JS and CSS shipped here change with deploys, and a cached HTML
    that hits a newer API can mismatch."""
    user = (await db.execute(
        select(User).where(User.booking_slug == slug, User.is_active == True)
    )).scalar_one_or_none()
    no_cache_headers = {"Cache-Control": "no-store, no-cache, must-revalidate"}
    if not user:
        return HTMLResponse(
            "<html><body style='font-family:system-ui;text-align:center;padding:60px'>"
            "<h2>Booking page not found</h2></body></html>",
            status_code=404, headers=no_cache_headers,
        )
    if not user.google_refresh_token:
        return HTMLResponse(
            "<html><body style='font-family:system-ui;text-align:center;padding:60px'>"
            "<h2>This page isn't ready yet</h2>"
            "<p>The host hasn't connected their calendar.</p></body></html>",
            status_code=503, headers=no_cache_headers,
        )
    # Brand resolution: per-user SchedulingConfig override → org brand.
    # The per-user fields were seeded with BMP defaults historically, so
    # we treat values matching the legacy defaults as "unset" and inherit
    # from org brand. This way an org brand change cascades to every
    # rep's booking page automatically — but reps who customized
    # explicitly keep their customization.
    from app.runtime_config import get_org_brand
    cfg = await _get_or_create_config(db, user.id)
    org = await get_org_brand(db)
    def _resolve(per_user, org_value, legacy_default):
        v = (per_user or "").strip()
        if not v or v.lower() == legacy_default.lower():
            return org_value
        return v
    brand = _resolve(cfg.brand_color, org["primary_color"], _DEFAULT_BRAND)
    accent = _resolve(cfg.accent_bg_color, org["accent_bg_color"], _DEFAULT_ACCENT_BG)
    logo_url = (cfg.logo_url or "").strip() or org["logo_url"]
    return HTMLResponse(
        _render_booking_html(slug, brand=brand, accent=accent, logo_url=logo_url),
        headers=no_cache_headers,
    )


def _render_booking_html(
    slug: str, *, brand: str = _DEFAULT_BRAND,
    accent: str = _DEFAULT_ACCENT_BG, logo_url: str = "",
) -> str:
    """The booking page template. Vanilla JS — no React/build step.
    Loads slots via /api/book/{slug}/info, groups by day in the
    visitor's local TZ, lets them pick a time, fills in name/email/
    phone, then POSTs to /confirm.

    Brand customization is server-rendered into CSS variables so the
    page paints in the right colors with no flash. Pass `brand`
    (primary), `accent` (soft tint backgrounds), and an optional
    `logo_url` (https only — http would trigger mixed-content blocks)."""
    # Build the slug + brand JSON literals once; the rest of the JS is
    # static. Using json.dumps preserves quoting + escapes.
    logo_block = (
        f'<img src="{logo_url}" alt="" style="max-height:48px;max-width:200px;'
        f'display:block;margin-bottom:12px" '
        f'onerror="this.style.display=\'none\'">'
    ) if logo_url and logo_url.startswith("https://") else ""
    return r"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Book a Discovery Call</title>
<style>
  :root {
    --brand: """ + brand + r""";
    --brand-soft: """ + accent + r""";
  }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background:#fafafa; color:#222; margin:0; padding:20px; }
  .wrap { max-width:780px; margin:30px auto; background:white; border-radius:14px; box-shadow:0 4px 20px rgba(0,0,0,0.06); overflow:hidden; }
  header { background:linear-gradient(135deg, var(--brand-soft), #fff); padding:28px 32px; border-bottom:1px solid #f1ebe5; }
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
  .slot-btn:hover { border-color:var(--brand); color:var(--brand); }
  .slot-btn.selected { background:var(--brand); color:white; border-color:var(--brand); }
  .form-pane { padding-left:14px; }
  @media (max-width:680px){ .form-pane { padding-left:0; } }
  .form-pane h3 { margin:0 0 10px; font-size:15px; }
  .form-pane label { display:block; font-size:12px; color:#666; margin-top:10px; }
  /* Scope text-input styling to only text-like inputs — otherwise radio/checkbox
     buttons get width:100% + padding + border applied and visually disappear. */
  .form-pane input:not([type="radio"]):not([type="checkbox"]), .form-pane textarea { width:100%; padding:9px 11px; border:1px solid #ddd; border-radius:6px; font-size:14px; box-sizing:border-box; font-family:inherit; }
  .form-pane input[type="radio"] { margin:0; cursor:pointer; }
  .form-pane .radio-group { margin-top:4px; }
  .form-pane .radio-option { display:flex; align-items:center; gap:8px; padding:6px 8px; margin-top:4px; border:1px solid transparent; border-radius:6px; font-size:13px; color:#333; cursor:pointer; transition:all 0.12s; }
  .form-pane .radio-option:hover { background:var(--brand-soft); }
  .form-pane .radio-option:has(input:checked) { background:var(--brand-soft); border-color:var(--brand); color:var(--brand); }
  .form-pane button { background:var(--brand); color:white; border:0; padding:11px 18px; border-radius:6px; font-size:14px; cursor:pointer; margin-top:14px; font-weight:600; }
  .form-pane button:disabled { background:#ccc; cursor:not-allowed; }
  .selected-summary { padding:10px 12px; background:var(--brand-soft); border-radius:6px; font-size:13px; margin-bottom:8px; color:#333; }
  .err { color:#c0392b; font-size:13px; margin-top:10px; }
  .empty { color:#888; font-size:13px; padding:20px; text-align:center; }
  .booked { text-align:center; padding:40px 20px; }
  .booked h2 { color:#1b5e20; }
  .booked .cta-link { background:var(--brand); }
</style>
</head>
<body>
  <div class="wrap" id="root">
    <header>
      """ + logo_block + r"""
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
function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
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
  const typeLabel = ({google_meet:'📹 Google Meet', phone:'📞 Phone', in_person:'📍 In person', custom_link:'🔗 Online'}[info.meeting_type] || '');
  document.getElementById('meeting-meta').textContent = `${info.meeting_title} · ${info.slot_minutes} min · ${typeLabel} · with ${info.host_name} · times shown in ${VIEWER_TZ}`;
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
  // Render any custom intake questions the host configured. Built-in
  // name/email/phone/message are always shown; custom ones append below
  // in their declared position order.
  const phoneRequiredByMeetingType = info.meeting_type === 'phone';
  const customQs = (info.questions || []).slice().sort((a,b) => (a.position||0) - (b.position||0));
  let customHtml = '';
  for (const q of customQs) {
    const id = 'bkq-' + q.key;
    const req = q.required ? ' *' : ' (optional)';
    if (q.type === 'long_text') {
      customHtml += `<label>${escapeHtml(q.label)}${req}</label><textarea id="${id}" data-qkey="${escapeHtml(q.key)}" rows="3"></textarea>`;
    } else if (q.type === 'url') {
      customHtml += `<label>${escapeHtml(q.label)}${req}</label><input id="${id}" data-qkey="${escapeHtml(q.key)}" type="url" placeholder="https://">`;
    } else if (q.type === 'single_select') {
      const opts = (q.options || []).map(o => `<label class="radio-option"><input type="radio" name="${id}" value="${escapeHtml(o)}" data-qkey="${escapeHtml(q.key)}"> <span>${escapeHtml(o)}</span></label>`).join('');
      const empty = !(q.options || []).length;
      customHtml += `<label>${escapeHtml(q.label)}${req}</label><div id="${id}" class="radio-group">${empty ? '<div style="font-size:11px;color:#888;font-style:italic">(no options configured yet)</div>' : opts}</div>`;
    } else {
      customHtml += `<label>${escapeHtml(q.label)}${req}</label><input id="${id}" data-qkey="${escapeHtml(q.key)}" type="text">`;
    }
  }

  html += `</div><div class="form-pane">
    <h3>Your details</h3>
    <div id="form-empty" style="color:#888;font-size:13px">Pick a time to book.</div>
    <div id="form-fields" style="display:none">
      <div class="selected-summary" id="selected-summary"></div>
      <label>Full name *</label><input id="bk-name" required>
      <label>Email *</label><input id="bk-email" type="email" required>
      <label>Phone${phoneRequiredByMeetingType ? ' *' : ' (optional)'}</label><input id="bk-phone" type="tel"${phoneRequiredByMeetingType ? ' required' : ''}>
      ${customHtml}
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

function _collectAnswers() {
  // Gather { question.key: value } from all rendered custom-question
  // inputs. For radio groups, we look up the checked input by name.
  const answers = {};
  for (const q of (info && info.questions) || []) {
    const id = 'bkq-' + q.key;
    if (q.type === 'single_select') {
      const checked = document.querySelector(`input[name="${id}"]:checked`);
      if (checked) answers[q.key] = checked.value;
    } else {
      const el = document.getElementById(id);
      if (el && el.value && el.value.trim()) answers[q.key] = el.value.trim();
    }
  }
  return answers;
}

async function confirmBooking() {
  const name = document.getElementById('bk-name').value.trim();
  const email = document.getElementById('bk-email').value.trim();
  const phone = document.getElementById('bk-phone').value.trim();
  const errEl = document.getElementById('bk-err');
  errEl.textContent = '';
  if (!name || !email) { errEl.textContent = 'Name and email are required.'; return; }
  if (!selectedSlot) { errEl.textContent = 'Please pick a time first.'; return; }
  if (info && info.meeting_type === 'phone' && !phone) {
    errEl.textContent = 'Phone is required — the host will call you.';
    return;
  }
  // Required custom-question check (we also re-validate server-side)
  for (const q of (info && info.questions) || []) {
    if (!q.required) continue;
    const id = 'bkq-' + q.key;
    let val = '';
    if (q.type === 'single_select') {
      const checked = document.querySelector(`input[name="${id}"]:checked`);
      val = checked ? checked.value : '';
    } else {
      const el = document.getElementById(id);
      val = el && el.value ? el.value.trim() : '';
    }
    if (!val) { errEl.textContent = `Required: ${q.label}`; return; }
  }
  const btn = document.getElementById('bk-submit');
  btn.disabled = true;
  btn.textContent = 'Booking…';
  const r = await fetch(`/api/book/${SLUG}/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      starts_at_utc: selectedSlot.utc,
      name, email, phone,
      viewer_timezone: VIEWER_TZ,
      answers: _collectAnswers(),
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
  // Build the meeting-location line shown on the confirmation screen.
  let locLine = '';
  if (data.meeting_type === 'google_meet' && data.google_meet_link) {
    locLine = `<p style="margin-top:14px"><a href="${escapeHtml(data.google_meet_link)}" style="display:inline-block;background:#1a73e8;color:white;padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:600">📹 Join Google Meet</a><br><span style="font-size:11px;color:#888">${escapeHtml(data.google_meet_link)}</span></p>`;
  } else if (data.meeting_type === 'phone') {
    locLine = `<p style="font-size:13px;color:#444;margin-top:14px">📞 The host will call you at the phone number you provided.</p>`;
  } else if (data.meeting_type === 'in_person') {
    locLine = `<p style="font-size:13px;color:#444;margin-top:14px">📍 Meeting location details are in your calendar invite.</p>`;
  } else if (data.meeting_type === 'custom_link') {
    locLine = `<p style="font-size:13px;color:#444;margin-top:14px">🔗 Meeting link is in your calendar invite.</p>`;
  }
  document.getElementById('root').innerHTML = `
    <div class="booked">
      <h2>You're booked!</h2>
      <p><strong>${fmtDay(selectedSlot.local)} at ${fmtTime(selectedSlot.local)}</strong></p>
      <p>A calendar invite is on its way to <strong>${escapeHtml(email)}</strong>.</p>
      ${locLine}
      ${data.google_event_link ? `<p style="margin-top:10px"><a href="${escapeHtml(data.google_event_link)}" style="color:var(--brand);font-size:12px">View on your calendar</a></p>` : ''}
      <p style="color:#888;font-size:13px;margin-top:18px">Talk soon,<br>${escapeHtml(data.host_name)}</p>
    </div>
  `;
}

load();
</script>
</body></html>
"""
