"""
Native scheduler — slot generation + booking helpers.

Pure logic in `generate_slots()`: takes a SchedulingConfig + busy
ranges + a time window, returns the list of bookable slot starts.
No I/O, easily unit-testable.

Wraps Google free-busy + event creation through `app.services.google_oauth`.
"""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Optional

from zoneinfo import ZoneInfo

from app.models import Booking, SchedulingConfig, User
from app.services.google_oauth import (
    GoogleAPIError, free_busy, refresh_access_token,
)

log = logging.getLogger("bmp.scheduler")


# ============================================================
# Defaults — used when a user has never customized their config
# ============================================================

DEFAULT_RULES = [
    # Mon–Fri 9am–12pm + 1pm–5pm, 1-hour lunch break
    {"weekday": 0, "start_time": "09:00", "end_time": "12:00"},
    {"weekday": 0, "start_time": "13:00", "end_time": "17:00"},
    {"weekday": 1, "start_time": "09:00", "end_time": "12:00"},
    {"weekday": 1, "start_time": "13:00", "end_time": "17:00"},
    {"weekday": 2, "start_time": "09:00", "end_time": "12:00"},
    {"weekday": 2, "start_time": "13:00", "end_time": "17:00"},
    {"weekday": 3, "start_time": "09:00", "end_time": "12:00"},
    {"weekday": 3, "start_time": "13:00", "end_time": "17:00"},
    {"weekday": 4, "start_time": "09:00", "end_time": "12:00"},
    {"weekday": 4, "start_time": "13:00", "end_time": "17:00"},
]


@dataclass
class Slot:
    starts_at: datetime  # tz-aware UTC
    ends_at: datetime    # tz-aware UTC

    def to_payload(self, viewer_tz: str) -> dict:
        tz = ZoneInfo(viewer_tz) if viewer_tz else timezone.utc
        return {
            "starts_at_utc": self.starts_at.isoformat(),
            "ends_at_utc": self.ends_at.isoformat(),
            "starts_at_local": self.starts_at.astimezone(tz).isoformat(),
            "ends_at_local": self.ends_at.astimezone(tz).isoformat(),
        }


# ============================================================
# Pure logic — generate slots from rules + busy ranges
# ============================================================

def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _ranges_overlap(a_start: datetime, a_end: datetime,
                    b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def generate_slots(
    config: SchedulingConfig,
    user_tz: str,
    *,
    window_start_utc: datetime,
    window_end_utc: datetime,
    busy_ranges: Iterable[tuple[datetime, datetime]] = (),
    booked_in_db: Iterable[tuple[datetime, datetime]] = (),
    now_utc: Optional[datetime] = None,
) -> list[Slot]:
    """Return all bookable slots in [window_start, window_end].

    A slot is bookable iff:
      - It falls within an availability rule for that weekday (host TZ)
      - It starts no sooner than now + min_lead_time_hours
      - It does not overlap any busy_range (Google) or booked_in_db
        (already-booked through us — defends against double-book if
         Google sync is laggy)
      - With buffer_before/after honored (effectively widen busy ranges)
      - Daily count <= daily_limit (when daily_limit > 0)

    All times are tz-aware. Internally we project rules into the
    user's timezone, then convert slot starts to UTC for output.
    """
    if not user_tz:
        user_tz = "America/Phoenix"
    tz = ZoneInfo(user_tz)
    now_utc = now_utc or datetime.now(timezone.utc)
    earliest_allowed = now_utc + timedelta(hours=max(0, config.min_lead_time_hours))

    rules = []
    if config.rules_json:
        try:
            rules = json.loads(config.rules_json) or []
        except Exception:
            rules = []
    if not rules:
        rules = DEFAULT_RULES

    # Widen busy ranges by buffer_before/after — a 5-min buffer means
    # we won't offer a slot that starts within 5 min of a busy range.
    pad_before = timedelta(minutes=max(0, config.buffer_before_minutes))
    pad_after = timedelta(minutes=max(0, config.buffer_after_minutes))
    padded_busy = [
        (b_start - pad_after, b_end + pad_before)
        for b_start, b_end in list(busy_ranges) + list(booked_in_db)
    ]

    slot_delta = timedelta(minutes=max(5, config.slot_minutes))
    out: list[Slot] = []
    daily_count: dict[date, int] = {}

    # Iterate days in user's local timezone
    cur_local_day = window_start_utc.astimezone(tz).date()
    end_local_day = window_end_utc.astimezone(tz).date()
    while cur_local_day <= end_local_day:
        weekday = cur_local_day.weekday()  # 0=Mon, 6=Sun
        for rule in rules:
            try:
                if int(rule.get("weekday", -1)) != weekday:
                    continue
                start_t = _parse_hhmm(rule["start_time"])
                end_t = _parse_hhmm(rule["end_time"])
            except Exception:
                continue
            window_start_local = datetime.combine(cur_local_day, start_t, tzinfo=tz)
            window_end_local = datetime.combine(cur_local_day, end_t, tzinfo=tz)
            cursor = window_start_local
            while cursor + slot_delta <= window_end_local:
                slot_start_utc = cursor.astimezone(timezone.utc)
                slot_end_utc = (cursor + slot_delta).astimezone(timezone.utc)
                # Apply window bounds
                if slot_end_utc <= window_start_utc:
                    cursor += slot_delta
                    continue
                if slot_start_utc >= window_end_utc:
                    break
                # Lead-time gate
                if slot_start_utc < earliest_allowed:
                    cursor += slot_delta
                    continue
                # Conflict check
                conflict = any(
                    _ranges_overlap(slot_start_utc, slot_end_utc, b0, b1)
                    for b0, b1 in padded_busy
                )
                if conflict:
                    cursor += slot_delta
                    continue
                # Daily limit
                local_day = cursor.date()
                if config.daily_limit and daily_count.get(local_day, 0) >= config.daily_limit:
                    cursor += slot_delta
                    continue
                out.append(Slot(starts_at=slot_start_utc, ends_at=slot_end_utc))
                daily_count[local_day] = daily_count.get(local_day, 0) + 1
                cursor += slot_delta
        cur_local_day += timedelta(days=1)

    return out


# ============================================================
# Google integration helpers
# ============================================================

async def fetch_user_busy(
    user: User,
    *,
    time_min: datetime,
    time_max: datetime,
) -> tuple[list[tuple[datetime, datetime]], Optional[str]]:
    """Pull free-busy from the user's PRIMARY calendar + their
    BMP Discovery Calls calendar (so already-booked discovery calls
    block new bookings even if they were created outside our flow).

    Returns (busy_ranges, error_message). `error_message` is a
    user-facing string when we couldn't reach Google — caller decides
    whether to fail open (show all slots) or fail closed (show
    none / "calendar temporarily unavailable")."""
    if not user.google_refresh_token:
        return [], "google_not_connected"
    try:
        tokens = await refresh_access_token(user.google_refresh_token)
    except GoogleAPIError as e:
        log.warning(f"Couldn't refresh Google token for user {user.id}: {e}")
        return [], "google_refresh_failed"
    cal_ids = ["primary"]
    if user.google_calendar_id and user.google_calendar_id != "primary":
        cal_ids.append(user.google_calendar_id)
    try:
        busy = await free_busy(tokens.access_token, cal_ids, time_min=time_min, time_max=time_max)
        return busy, None
    except GoogleAPIError as e:
        log.warning(f"Couldn't fetch free-busy for user {user.id}: {e}")
        return [], "google_freebusy_failed"


async def db_busy_ranges(
    db, host_user_id: int, *, time_min: datetime, time_max: datetime,
) -> list[tuple[datetime, datetime]]:
    """Confirmed bookings in our DB. Defense-in-depth against Google
    sync lag — even if free-busy is stale, we won't double-book a slot
    we ourselves wrote."""
    from sqlalchemy import select
    rows = (await db.execute(
        select(Booking).where(
            Booking.host_user_id == host_user_id,
            Booking.status == "confirmed",
            Booking.starts_at < time_max,
            Booking.ends_at > time_min,
        )
    )).scalars().all()
    out: list[tuple[datetime, datetime]] = []
    for b in rows:
        s = b.starts_at if b.starts_at.tzinfo else b.starts_at.replace(tzinfo=timezone.utc)
        e = b.ends_at if b.ends_at.tzinfo else b.ends_at.replace(tzinfo=timezone.utc)
        out.append((s, e))
    return out
