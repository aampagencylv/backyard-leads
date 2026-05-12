"""
Autopilot send-window logic.

One service that owns:
  - Reading the configured window from RuntimeConfig (default 8am-7pm,
    every day).
  - Inferring the contact's local timezone (phone area code → company
    state → rep's timezone → America/Los_Angeles fallback).
  - Deciding whether *now* is inside the window for a given contact.
  - Computing the next valid window-start datetime so the engine /
    sequence generator can snap to it.

This replaces the older hardcoded 8am-9pm constants in twilio_sms.py.
Callers should use these helpers instead of going direct.

For iMessage/SMS we additionally clamp to TCPA's 8am-9pm — if an admin
sets a weirder window we never exceed legal bounds. Email isn't TCPA-
regulated so the configured window applies as-is.
"""
from __future__ import annotations
import json
import logging
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RuntimeConfig, Contact, Company, User
from app.services.twilio_sms import _infer_timezone as _infer_tz_from_phone

log = logging.getLogger("bmp.send_window")

# Static fallback for area codes we don't recognize / non-US numbers /
# email-only contacts with no phone + no company state + no rep TZ.
FALLBACK_TZ = "America/Los_Angeles"

# TCPA cap — even if admin sets a weirder window, SMS/iMessage never
# go outside this in contact-local time.
TCPA_START_HOUR = 8
TCPA_END_HOUR = 21


# Coarse US-state → IANA timezone map. Enough states that 50-state
# coverage is intact; this is the fallback path when we don't have a
# phone area code to look at.
STATE_TZ = {
    "WA": "America/Los_Angeles", "OR": "America/Los_Angeles", "CA": "America/Los_Angeles",
    "NV": "America/Los_Angeles",
    "AZ": "America/Phoenix",  # no DST
    "ID": "America/Denver", "MT": "America/Denver", "WY": "America/Denver",
    "UT": "America/Denver", "CO": "America/Denver", "NM": "America/Denver",
    "ND": "America/Chicago", "SD": "America/Chicago", "NE": "America/Chicago",
    "KS": "America/Chicago", "OK": "America/Chicago", "TX": "America/Chicago",
    "MN": "America/Chicago", "IA": "America/Chicago", "MO": "America/Chicago",
    "AR": "America/Chicago", "LA": "America/Chicago", "WI": "America/Chicago",
    "IL": "America/Chicago", "MS": "America/Chicago", "AL": "America/Chicago",
    "TN": "America/Chicago", "KY": "America/Chicago",
    "MI": "America/New_York", "IN": "America/New_York", "OH": "America/New_York",
    "GA": "America/New_York", "FL": "America/New_York", "SC": "America/New_York",
    "NC": "America/New_York", "VA": "America/New_York", "WV": "America/New_York",
    "PA": "America/New_York", "NY": "America/New_York", "NJ": "America/New_York",
    "CT": "America/New_York", "RI": "America/New_York", "MA": "America/New_York",
    "VT": "America/New_York", "NH": "America/New_York", "ME": "America/New_York",
    "MD": "America/New_York", "DE": "America/New_York", "DC": "America/New_York",
    "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
}


# ============================================================
# Config
# ============================================================

@dataclass
class SendWindow:
    start_hour: int   # 0..23
    end_hour: int     # 1..24 (exclusive)
    weekdays: set[int]  # 0=Mon..6=Sun


async def get_send_window(db: AsyncSession) -> SendWindow:
    """Read the active send-window config from runtime_config. Falls
    back to 8am-7pm every day when no row exists yet."""
    rc = (await db.execute(select(RuntimeConfig).where(RuntimeConfig.id == 1))).scalar_one_or_none()
    start = int(getattr(rc, "autopilot_send_start_hour", None) or 8) if rc else 8
    end = int(getattr(rc, "autopilot_send_end_hour", None) or 19) if rc else 19
    start = max(0, min(23, start))
    end = max(start + 1, min(24, end))
    days_raw = getattr(rc, "autopilot_send_days_json", None) if rc else None
    weekdays: set[int]
    if days_raw:
        try:
            parsed = json.loads(days_raw)
            weekdays = {int(d) for d in parsed if 0 <= int(d) <= 6}
            if not weekdays:
                weekdays = {0, 1, 2, 3, 4, 5, 6}
        except (ValueError, TypeError):
            weekdays = {0, 1, 2, 3, 4, 5, 6}
    else:
        weekdays = {0, 1, 2, 3, 4, 5, 6}
    return SendWindow(start_hour=start, end_hour=end, weekdays=weekdays)


# ============================================================
# Timezone inference
# ============================================================

def infer_contact_timezone(
    contact: Optional[Contact],
    company: Optional[Company],
    rep: Optional[User] = None,
) -> str:
    """Best-effort contact timezone:
       1. Phone area code (US E.164 → AREA_CODE_TZ).
       2. Company state (US two-letter → STATE_TZ).
       3. Rep's saved timezone (User.timezone).
       4. America/Los_Angeles (BMP's home market).
    """
    # 1. Phone
    if contact and contact.phone:
        try:
            tz = _infer_tz_from_phone(contact.phone)
            if tz and tz != FALLBACK_TZ:
                return tz
            # phone existed but unknown area code → fall through but
            # remember we got the default
            phone_default = tz
        except Exception:
            phone_default = None
    else:
        phone_default = None

    # 2. Company state
    if company and company.state:
        s = (company.state or "").strip().upper()[:2]
        if s in STATE_TZ:
            return STATE_TZ[s]

    # 3. Rep TZ
    if rep:
        rep_tz = getattr(rep, "timezone", None)
        if rep_tz:
            try:
                zoneinfo.ZoneInfo(rep_tz)
                return rep_tz
            except zoneinfo.ZoneInfoNotFoundError:
                pass

    # 4. Hard fallback (or the phone's "we don't know" default)
    return phone_default or FALLBACK_TZ


# ============================================================
# Window check + next-slot math
# ============================================================

def _effective_bounds(window: SendWindow, channel: str) -> tuple[int, int]:
    """SMS/iMessage clamp to TCPA 8-21 even if admin widened the
    org window. Email is not regulated by TCPA so the org window applies."""
    start, end = window.start_hour, window.end_hour
    if channel in ("imessage", "sms"):
        start = max(start, TCPA_START_HOUR)
        end = min(end, TCPA_END_HOUR)
    return start, end


def is_within_window(
    *,
    now_utc: datetime,
    contact_tz: str,
    window: SendWindow,
    channel: str = "email",
) -> bool:
    try:
        tz = zoneinfo.ZoneInfo(contact_tz)
    except zoneinfo.ZoneInfoNotFoundError:
        tz = zoneinfo.ZoneInfo(FALLBACK_TZ)
    local = now_utc.astimezone(tz)
    if local.weekday() not in window.weekdays:
        return False
    start_h, end_h = _effective_bounds(window, channel)
    return start_h <= local.hour < end_h


def next_window_start(
    *,
    after_utc: datetime,
    contact_tz: str,
    window: SendWindow,
    channel: str = "email",
) -> datetime:
    """Return the UTC datetime of the next valid window-start (>= after_utc),
    in the contact's local timezone."""
    try:
        tz = zoneinfo.ZoneInfo(contact_tz)
    except zoneinfo.ZoneInfoNotFoundError:
        tz = zoneinfo.ZoneInfo(FALLBACK_TZ)
    local = after_utc.astimezone(tz)
    start_h, end_h = _effective_bounds(window, channel)
    # Walk forward day by day until we find one where the start hour
    # in local time is >= after_utc AND it's an allowed weekday.
    for day_offset in range(0, 8):
        candidate = local.replace(hour=start_h, minute=0, second=0, microsecond=0) + timedelta(days=day_offset)
        if candidate.weekday() not in window.weekdays:
            continue
        # Same-day case: today's start hour may have passed and we're
        # still inside the window — just send now.
        if day_offset == 0 and start_h <= local.hour < end_h:
            return after_utc
        if candidate >= local:
            return candidate.astimezone(timezone.utc)
    # 8 days searched and nothing landed (admin set empty weekdays?) —
    # fall back to "right now" so we don't infinite-loop. The engine
    # will re-check the config on the next tick.
    return after_utc


# ============================================================
# High-level helpers used by callers
# ============================================================

async def snap_to_window(
    db: AsyncSession,
    *,
    desired_utc: datetime,
    contact: Optional[Contact],
    company: Optional[Company],
    rep: Optional[User] = None,
    channel: str = "email",
) -> datetime:
    """Given a desired send time, return either the same time (if it
    falls in the window) or the next valid window-start. Used by the
    sequence generator when laying out step schedules so the calendar
    shows realistic times instead of midnight queueings."""
    window = await get_send_window(db)
    contact_tz = infer_contact_timezone(contact, company, rep)
    if is_within_window(now_utc=desired_utc, contact_tz=contact_tz, window=window, channel=channel):
        return desired_utc
    return next_window_start(after_utc=desired_utc, contact_tz=contact_tz, window=window, channel=channel)


async def snap_pending_steps_to_window(
    db: AsyncSession,
    *,
    contact_id: int,
) -> int:
    """Walk all unsent auto-execute steps for a contact and rewrite
    their scheduled_send_at to the next valid window-start. Called at
    the end of every sequence-generation flow so the UI / calendar
    show realistic send times instead of midnight queueings.

    Returns the number of steps that were snapped (i.e. had to move).
    Idempotent — calling twice in a row is a no-op the second time.
    """
    # Avoid circular import: GeneratedEmail / Contact / Company / User
    # are already available via app.models, but the engine pulls in
    # this module too, so we import lazily here.
    from app.models import GeneratedEmail, Contact, Company, User
    from sqlalchemy import select as _sel

    contact = (await db.execute(_sel(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        return 0
    company = (await db.execute(_sel(Company).where(Company.id == contact.company_id))).scalar_one_or_none()
    rep = None
    if company and company.assigned_to:
        rep = (await db.execute(_sel(User).where(User.id == company.assigned_to))).scalar_one_or_none()

    window = await get_send_window(db)
    contact_tz = infer_contact_timezone(contact, company, rep)

    steps = (await db.execute(
        _sel(GeneratedEmail).where(
            GeneratedEmail.contact_id == contact_id,
            GeneratedEmail.is_sent == False,
            GeneratedEmail.paused_at.is_(None),
            GeneratedEmail.skipped_at.is_(None),
            GeneratedEmail.scheduled_send_at.isnot(None),
        )
    )).scalars().all()

    snapped = 0
    for step in steps:
        # Email is non-TCPA but uses org window. iMessage is clamped.
        # Other step types (call, linkedin) are tasks for humans — no
        # window applies; skip them.
        channel = step.step_type if step.step_type in ("email", "imessage") else None
        if not channel:
            continue
        original = step.scheduled_send_at
        if original.tzinfo is None:
            # SQLite strips tzinfo — coerce to UTC for the math
            original = original.replace(tzinfo=timezone.utc)
        if is_within_window(now_utc=original, contact_tz=contact_tz, window=window, channel=channel):
            continue
        new_time = next_window_start(after_utc=original, contact_tz=contact_tz, window=window, channel=channel)
        step.scheduled_send_at = new_time
        snapped += 1

    return snapped


async def is_now_sendable(
    db: AsyncSession,
    *,
    contact: Optional[Contact],
    company: Optional[Company],
    rep: Optional[User] = None,
    channel: str = "email",
    now_utc: Optional[datetime] = None,
) -> tuple[bool, str]:
    """For the engine: is *right now* within the window for this
    contact? Returns (allowed, human_reason). The reason text is shown
    in defer-log entries."""
    now = now_utc or datetime.now(timezone.utc)
    window = await get_send_window(db)
    contact_tz = infer_contact_timezone(contact, company, rep)
    if is_within_window(now_utc=now, contact_tz=contact_tz, window=window, channel=channel):
        return True, ""
    try:
        tz = zoneinfo.ZoneInfo(contact_tz)
    except zoneinfo.ZoneInfoNotFoundError:
        tz = zoneinfo.ZoneInfo(FALLBACK_TZ)
    local = now.astimezone(tz)
    start_h, end_h = _effective_bounds(window, channel)
    return False, (
        f"outside send window ({start_h:02d}:00-{end_h:02d}:00 "
        f"{contact_tz.split('/')[-1].replace('_', ' ')}) — "
        f"contact local time is {local.strftime('%a %I:%M %p')}"
    )
