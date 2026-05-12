"""
Autopilot send-window logic — per-channel, with basis radio.

Tenants pick a *basis*:
  - "contact"   — hours apply in the contact's local timezone (default).
  - "rep"       — hours apply in the assigned rep's saved timezone.
  - "strictest" — only fire when *both* the contact-window AND the
                  rep-window are open simultaneously. The right choice
                  when you want a human available to reply (e.g. iMessage
                  from a Phoenix rep to a NYC contact at 6pm ET: contact
                  is in window, rep is off — strictest blocks it).

Per-channel hours: email defaults 8am-7pm, iMessage defaults 8am-5pm
(narrower because someone needs to be at a keyboard to handle replies).
iMessage/SMS additionally clamp to TCPA's 8am-9pm contact-local
regardless of admin config.

Timezone inference for contacts (best-effort, in order):
  1. Phone area code (US E.164 → AREA_CODE_TZ).
  2. Company state (US two-letter → STATE_TZ).
  3. Rep's saved timezone.
  4. America/Los_Angeles (BMP's home market).

Rep timezone inference:
  1. User.timezone field.
  2. America/Los_Angeles fallback.
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

FALLBACK_TZ = "America/Los_Angeles"

# TCPA cap — even if admin sets a weirder window, iMessage/SMS never
# go outside this in contact-local time.
TCPA_START_HOUR = 8
TCPA_END_HOUR = 21


# US-state → IANA timezone fallback when we don't have a phone area code.
STATE_TZ = {
    "WA": "America/Los_Angeles", "OR": "America/Los_Angeles", "CA": "America/Los_Angeles",
    "NV": "America/Los_Angeles",
    "AZ": "America/Phoenix",
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


@dataclass
class ChannelWindow:
    start_hour: int           # 0..23
    end_hour: int             # 1..24 (exclusive)
    weekdays: set[int]        # 0=Mon..6=Sun


@dataclass
class AutopilotConfig:
    basis: str                # contact | rep | strictest
    email: ChannelWindow
    imessage: ChannelWindow
    respect_rep_presence: bool


# ============================================================
# Config read
# ============================================================

def _parse_days(raw: Optional[str]) -> set[int]:
    if not raw:
        return {0, 1, 2, 3, 4, 5, 6}
    try:
        parsed = json.loads(raw)
        days = {int(d) for d in parsed if 0 <= int(d) <= 6}
        return days or {0, 1, 2, 3, 4, 5, 6}
    except (ValueError, TypeError):
        return {0, 1, 2, 3, 4, 5, 6}


def _clamp_hours(start: int, end: int, defaults: tuple[int, int]) -> tuple[int, int]:
    """Coerce to 0-23/1-24 ranges with start<end. Fall back to defaults if invalid."""
    try:
        s = int(start)
        e = int(end)
    except (TypeError, ValueError):
        return defaults
    s = max(0, min(23, s))
    e = max(s + 1, min(24, e))
    return (s, e)


async def get_autopilot_config(db: AsyncSession) -> AutopilotConfig:
    rc = (await db.execute(select(RuntimeConfig).where(RuntimeConfig.id == 1))).scalar_one_or_none()
    if rc is None:
        return AutopilotConfig(
            basis="contact",
            email=ChannelWindow(8, 19, {0,1,2,3,4,5,6}),
            imessage=ChannelWindow(8, 17, {0,1,2,3,4,5,6}),
            respect_rep_presence=False,
        )
    basis = (getattr(rc, "autopilot_basis", None) or "contact").strip().lower()
    if basis not in ("contact", "rep", "strictest"):
        basis = "contact"

    es, ee = _clamp_hours(
        getattr(rc, "autopilot_email_start_hour", 8),
        getattr(rc, "autopilot_email_end_hour", 19),
        (8, 19),
    )
    ms, me = _clamp_hours(
        getattr(rc, "autopilot_imessage_start_hour", 8),
        getattr(rc, "autopilot_imessage_end_hour", 17),
        (8, 17),
    )
    email = ChannelWindow(es, ee, _parse_days(getattr(rc, "autopilot_email_days_json", None)))
    imessage = ChannelWindow(ms, me, _parse_days(getattr(rc, "autopilot_imessage_days_json", None)))
    return AutopilotConfig(
        basis=basis,
        email=email,
        imessage=imessage,
        respect_rep_presence=bool(getattr(rc, "autopilot_respect_rep_presence", False)),
    )


def channel_window(cfg: AutopilotConfig, channel: str) -> ChannelWindow:
    """Pick the right per-channel window, defaulting to email's window
    for unknown channels (SMS routes through iMessage rules)."""
    if channel == "imessage" or channel == "sms":
        return cfg.imessage
    return cfg.email


# Legacy adapter — older callers asked for a single SendWindow. Returns
# the email window since that was the previous behavior for the org-wide
# autopilot_send_* columns.
@dataclass
class SendWindow:
    start_hour: int
    end_hour: int
    weekdays: set[int]


async def get_send_window(db: AsyncSession, channel: str = "email") -> SendWindow:
    cfg = await get_autopilot_config(db)
    w = channel_window(cfg, channel)
    return SendWindow(start_hour=w.start_hour, end_hour=w.end_hour, weekdays=set(w.weekdays))


# ============================================================
# Timezone inference
# ============================================================

def _safe_zone(name: Optional[str]) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(name or FALLBACK_TZ)
    except zoneinfo.ZoneInfoNotFoundError:
        return zoneinfo.ZoneInfo(FALLBACK_TZ)


def infer_contact_timezone(
    contact: Optional[Contact],
    company: Optional[Company],
    rep: Optional[User] = None,
) -> str:
    phone_default = None
    if contact and contact.phone:
        try:
            tz = _infer_tz_from_phone(contact.phone)
            if tz and tz != FALLBACK_TZ:
                return tz
            phone_default = tz
        except Exception:
            phone_default = None
    if company and company.state:
        s = (company.state or "").strip().upper()[:2]
        if s in STATE_TZ:
            return STATE_TZ[s]
    if rep:
        rep_tz = getattr(rep, "timezone", None)
        if rep_tz:
            try:
                zoneinfo.ZoneInfo(rep_tz)
                return rep_tz
            except zoneinfo.ZoneInfoNotFoundError:
                pass
    return phone_default or FALLBACK_TZ


def infer_rep_timezone(rep: Optional[User]) -> str:
    if rep and getattr(rep, "timezone", None):
        try:
            zoneinfo.ZoneInfo(rep.timezone)
            return rep.timezone
        except zoneinfo.ZoneInfoNotFoundError:
            pass
    return FALLBACK_TZ


# ============================================================
# Per-side window logic + TCPA clamp
# ============================================================

def _effective_bounds(window: ChannelWindow, channel: str) -> tuple[int, int]:
    start, end = window.start_hour, window.end_hour
    if channel in ("imessage", "sms"):
        start = max(start, TCPA_START_HOUR)
        end = min(end, TCPA_END_HOUR)
    return start, end


def _hour_is_inside(window: ChannelWindow, channel: str, local: datetime) -> bool:
    if local.weekday() not in window.weekdays:
        return False
    s, e = _effective_bounds(window, channel)
    return s <= local.hour < e


# ============================================================
# Public check + next-slot
# ============================================================

def is_within_window(
    *,
    now_utc: datetime,
    contact_tz: str,
    rep_tz: Optional[str],
    cfg: AutopilotConfig,
    channel: str = "email",
) -> bool:
    window = channel_window(cfg, channel)
    contact_zone = _safe_zone(contact_tz)
    contact_local = now_utc.astimezone(contact_zone)
    contact_ok = _hour_is_inside(window, channel, contact_local)
    if cfg.basis == "contact":
        return contact_ok
    rep_zone = _safe_zone(rep_tz)
    rep_local = now_utc.astimezone(rep_zone)
    rep_ok = _hour_is_inside(window, channel, rep_local)
    if cfg.basis == "rep":
        return rep_ok
    # strictest
    return contact_ok and rep_ok


def next_window_start(
    *,
    after_utc: datetime,
    contact_tz: str,
    rep_tz: Optional[str],
    cfg: AutopilotConfig,
    channel: str = "email",
) -> datetime:
    """First UTC datetime >= after_utc at which the configured window
    is open. Walks forward in 30-min steps for up to 8 days (worst-case
    'strictest of both' across far-apart timezones with restrictive
    weekday config)."""
    candidate = after_utc.replace(minute=0, second=0, microsecond=0)
    # If we're already inside, return now
    if is_within_window(
        now_utc=after_utc, contact_tz=contact_tz, rep_tz=rep_tz,
        cfg=cfg, channel=channel,
    ):
        return after_utc

    # Walk hour-by-hour. 8 days × 24 hrs = 192 max iterations.
    max_steps = 8 * 24
    step = candidate
    if step < after_utc:
        step = after_utc.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    elif step == after_utc.replace(minute=0, second=0, microsecond=0):
        step = step + timedelta(hours=1)
    for _ in range(max_steps):
        if is_within_window(
            now_utc=step, contact_tz=contact_tz, rep_tz=rep_tz,
            cfg=cfg, channel=channel,
        ):
            return step
        step = step + timedelta(hours=1)
    # 8 days searched and nothing matched — fall back to after_utc.
    # Either the windows are empty (no weekdays selected, both basis
    # config impossible) or the configured windows never overlap.
    log.warning(
        "next_window_start exhausted 8-day search — falling back to after_utc "
        f"basis={cfg.basis} channel={channel} contact_tz={contact_tz} rep_tz={rep_tz}"
    )
    return after_utc


# ============================================================
# High-level helpers used by callers
# ============================================================

async def is_now_sendable(
    db: AsyncSession,
    *,
    contact: Optional[Contact],
    company: Optional[Company],
    rep: Optional[User] = None,
    channel: str = "email",
    now_utc: Optional[datetime] = None,
) -> tuple[bool, str]:
    """For the engine: is *right now* within the window for this contact
    on this channel? Returns (allowed, human_reason)."""
    now = now_utc or datetime.now(timezone.utc)
    cfg = await get_autopilot_config(db)
    contact_tz = infer_contact_timezone(contact, company, rep)
    rep_tz = infer_rep_timezone(rep)

    if is_within_window(
        now_utc=now, contact_tz=contact_tz, rep_tz=rep_tz, cfg=cfg, channel=channel,
    ):
        return True, ""

    window = channel_window(cfg, channel)
    s, e = _effective_bounds(window, channel)
    contact_local = now.astimezone(_safe_zone(contact_tz))
    rep_local = now.astimezone(_safe_zone(rep_tz))
    if cfg.basis == "rep":
        return False, (
            f"outside rep send window ({s:02d}:00-{e:02d}:00 "
            f"{rep_tz.split('/')[-1].replace('_', ' ')}) — rep local time is "
            f"{rep_local.strftime('%a %I:%M %p')}"
        )
    if cfg.basis == "strictest":
        return False, (
            f"outside send window ({s:02d}:00-{e:02d}:00) — strictest-of-both; "
            f"contact {contact_local.strftime('%a %I:%M %p')} {contact_tz.split('/')[-1]}; "
            f"rep {rep_local.strftime('%a %I:%M %p')} {rep_tz.split('/')[-1]}"
        )
    return False, (
        f"outside contact send window ({s:02d}:00-{e:02d}:00 "
        f"{contact_tz.split('/')[-1].replace('_', ' ')}) — contact local time is "
        f"{contact_local.strftime('%a %I:%M %p')}"
    )


async def snap_to_window(
    db: AsyncSession,
    *,
    desired_utc: datetime,
    contact: Optional[Contact],
    company: Optional[Company],
    rep: Optional[User] = None,
    channel: str = "email",
) -> datetime:
    cfg = await get_autopilot_config(db)
    contact_tz = infer_contact_timezone(contact, company, rep)
    rep_tz = infer_rep_timezone(rep)
    if is_within_window(
        now_utc=desired_utc, contact_tz=contact_tz, rep_tz=rep_tz,
        cfg=cfg, channel=channel,
    ):
        return desired_utc
    return next_window_start(
        after_utc=desired_utc, contact_tz=contact_tz, rep_tz=rep_tz,
        cfg=cfg, channel=channel,
    )


async def snap_pending_steps_to_window(
    db: AsyncSession,
    *,
    contact_id: int,
) -> int:
    from app.models import GeneratedEmail
    from sqlalchemy import select as _sel

    contact = (await db.execute(_sel(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        return 0
    company = (await db.execute(_sel(Company).where(Company.id == contact.company_id))).scalar_one_or_none()
    rep = None
    if company and company.assigned_to:
        rep = (await db.execute(_sel(User).where(User.id == company.assigned_to))).scalar_one_or_none()

    cfg = await get_autopilot_config(db)
    contact_tz = infer_contact_timezone(contact, company, rep)
    rep_tz = infer_rep_timezone(rep)

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
        channel = step.step_type if step.step_type in ("email", "imessage") else None
        if not channel:
            continue
        original = step.scheduled_send_at
        if original.tzinfo is None:
            original = original.replace(tzinfo=timezone.utc)
        if is_within_window(
            now_utc=original, contact_tz=contact_tz, rep_tz=rep_tz,
            cfg=cfg, channel=channel,
        ):
            continue
        new_time = next_window_start(
            after_utc=original, contact_tz=contact_tz, rep_tz=rep_tz,
            cfg=cfg, channel=channel,
        )
        step.scheduled_send_at = new_time
        snapped += 1
    return snapped


# ============================================================
# Preview — used by the Settings page "would this fire?" widget
# ============================================================

async def preview_for_contact(
    db: AsyncSession,
    *,
    contact_id: int,
    channel: str,
    now_utc: Optional[datetime] = None,
) -> dict:
    """Returns enough info for the UI to say 'would fire now' or
    'deferred to <local time>' for any contact + channel."""
    now = now_utc or datetime.now(timezone.utc)
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        return {"found": False}
    company = (await db.execute(select(Company).where(Company.id == contact.company_id))).scalar_one_or_none()
    rep = None
    if company and company.assigned_to:
        rep = (await db.execute(select(User).where(User.id == company.assigned_to))).scalar_one_or_none()

    cfg = await get_autopilot_config(db)
    contact_tz = infer_contact_timezone(contact, company, rep)
    rep_tz = infer_rep_timezone(rep)

    allowed, reason = await is_now_sendable(
        db, contact=contact, company=company, rep=rep, channel=channel, now_utc=now,
    )
    contact_local = now.astimezone(_safe_zone(contact_tz))
    rep_local = now.astimezone(_safe_zone(rep_tz)) if rep else None
    next_utc = None if allowed else next_window_start(
        after_utc=now, contact_tz=contact_tz, rep_tz=rep_tz,
        cfg=cfg, channel=channel,
    )
    next_contact_local = (
        next_utc.astimezone(_safe_zone(contact_tz)).isoformat() if next_utc else None
    )
    return {
        "found": True,
        "contact_name": contact.full_name or contact.email or f"#{contact.id}",
        "company_name": company.name if company else None,
        "rep_name": (rep.full_name or rep.email) if rep else None,
        "channel": channel,
        "basis": cfg.basis,
        "contact_tz": contact_tz,
        "rep_tz": rep_tz,
        "contact_local_now": contact_local.isoformat(),
        "rep_local_now": rep_local.isoformat() if rep_local else None,
        "allowed": allowed,
        "reason": reason,
        "next_send_utc": next_utc.isoformat() if next_utc else None,
        "next_send_contact_local": next_contact_local,
    }
