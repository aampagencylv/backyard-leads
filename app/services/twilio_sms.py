"""
Twilio SMS service — outbound + inbound + TCPA compliance.

Send rules baked in:
  - STOP / UNSUBSCRIBE / OPT-OUT / CANCEL / END / QUIT keywords from a
    contact auto-set Contact.do_not_text and refuse future sends.
  - Send-window: 8am-9pm in the contact's local timezone (best-effort
    inferred from the contact's area code; defaults to America/Los_Angeles
    when we can't tell).
  - Length: SMS auto-segments at 160/153 chars per Twilio's standard.

A2P 10DLC compliance is a separate registration flow handled OUTSIDE
this code (Twilio Trust Hub or A2P Wizard). Once your numbers are
registered to a TCR campaign, they send at full throughput. Until then,
Twilio rate-limits + adds unregistered-traffic fees but the calls still
go through.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timezone
from typing import Optional
import re
import zoneinfo

import httpx

from app.services.twilio_voice import (
    TwilioCredentials,
    TwilioError,
    normalize_phone_e164,
    TWILIO_BASE,
    _auth,
)


# Keywords that must trigger an opt-out per TCPA + carrier guidelines.
STOP_KEYWORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "OPT-OUT", "OPTOUT", "CANCEL", "END", "QUIT", "REVOKE"}
START_KEYWORDS = {"START", "UNSTOP", "RESUME"}

# Permitted send window (local time of the recipient).
SEND_WINDOW_START = dt_time(8, 0)
SEND_WINDOW_END = dt_time(21, 0)

# Quick US area code → IANA timezone map for the markets BMP cares about.
# This is a heuristic, not authoritative — for 100% accuracy we'd need a
# carrier lookup. Good enough for compliance-with-good-faith-effort.
AREA_CODE_TZ = {
    # Pacific
    "206": "America/Los_Angeles", "253": "America/Los_Angeles", "360": "America/Los_Angeles",
    "415": "America/Los_Angeles", "510": "America/Los_Angeles", "650": "America/Los_Angeles",
    "707": "America/Los_Angeles", "805": "America/Los_Angeles", "818": "America/Los_Angeles",
    "858": "America/Los_Angeles", "909": "America/Los_Angeles", "925": "America/Los_Angeles",
    "949": "America/Los_Angeles", "503": "America/Los_Angeles", "971": "America/Los_Angeles",
    "971": "America/Los_Angeles",
    # Mountain (no DST in AZ)
    "480": "America/Phoenix", "520": "America/Phoenix", "602": "America/Phoenix",
    "623": "America/Phoenix", "928": "America/Phoenix",
    # Mountain (with DST)
    "303": "America/Denver", "720": "America/Denver", "970": "America/Denver",
    "801": "America/Denver", "385": "America/Denver",
    # Nevada (Pacific)
    "702": "America/Los_Angeles", "725": "America/Los_Angeles", "775": "America/Los_Angeles",
    # Central
    "210": "America/Chicago", "214": "America/Chicago", "281": "America/Chicago",
    "312": "America/Chicago", "346": "America/Chicago", "469": "America/Chicago",
    "512": "America/Chicago", "713": "America/Chicago", "737": "America/Chicago",
    "832": "America/Chicago", "936": "America/Chicago", "972": "America/Chicago",
    # Eastern (Florida, NY, etc.)
    "212": "America/New_York", "305": "America/New_York", "321": "America/New_York",
    "407": "America/New_York", "561": "America/New_York", "646": "America/New_York",
    "718": "America/New_York", "754": "America/New_York", "786": "America/New_York",
    "813": "America/New_York", "917": "America/New_York", "954": "America/New_York",
}


def _infer_timezone(e164_number: str) -> str:
    """Return IANA tz string for a US E.164 number based on area code, or
    default to Los_Angeles when we can't tell (BMP's home market)."""
    if not e164_number or not e164_number.startswith("+1") or len(e164_number) < 5:
        return "America/Los_Angeles"
    area = e164_number[2:5]
    return AREA_CODE_TZ.get(area, "America/Los_Angeles")


def is_stop_keyword(body: str) -> bool:
    if not body:
        return False
    word = body.strip().upper().split()[0] if body.strip() else ""
    return word in STOP_KEYWORDS


def is_start_keyword(body: str) -> bool:
    if not body:
        return False
    word = body.strip().upper().split()[0] if body.strip() else ""
    return word in START_KEYWORDS


@dataclass
class SendWindowCheck:
    allowed: bool
    reason: str = ""
    contact_local_now: Optional[datetime] = None


def check_send_window(to_number: str, now_utc: Optional[datetime] = None) -> SendWindowCheck:
    """
    Returns whether it's currently within the contact's local send window
    (8am-9pm). Don't send outside this window — TCPA explicitly prohibits
    calls/texts before 8am or after 9pm in the recipient's local time.
    """
    now = now_utc or datetime.now(timezone.utc)
    tz_name = _infer_timezone(to_number)
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except zoneinfo.ZoneInfoNotFoundError:
        tz = zoneinfo.ZoneInfo("America/Los_Angeles")
    local = now.astimezone(tz)
    t = local.time()
    if SEND_WINDOW_START <= t < SEND_WINDOW_END:
        return SendWindowCheck(allowed=True, contact_local_now=local)
    return SendWindowCheck(
        allowed=False,
        reason=f"It's {local.strftime('%I:%M %p')} {tz_name.split('/')[-1]} where this contact lives. Send window is 8am–9pm local.",
        contact_local_now=local,
    )


@dataclass
class SmsSendResult:
    success: bool
    message_sid: Optional[str] = None
    error: Optional[str] = None
    status_code: Optional[int] = None


async def send_sms(
    creds: TwilioCredentials,
    to_number: str,
    from_number: str,
    body: str,
    status_callback: Optional[str] = None,
) -> SmsSendResult:
    """Send an SMS via Twilio's Messages API."""
    if not creds.is_minimally_configured:
        return SmsSendResult(False, error="Twilio not configured")
    to_e164 = normalize_phone_e164(to_number)
    from_e164 = normalize_phone_e164(from_number)
    if not (to_e164 and from_e164):
        return SmsSendResult(False, error=f"Invalid phone numbers: to={to_number}, from={from_number}")

    payload = {"To": to_e164, "From": from_e164, "Body": body}
    if status_callback:
        payload["StatusCallback"] = status_callback

    url = f"{TWILIO_BASE}/Accounts/{creds.account_sid}/Messages.json"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(url, data=payload, auth=_auth(creds))
        except httpx.HTTPError as e:
            return SmsSendResult(False, error=f"Network error: {e}")

    if r.status_code in (200, 201):
        return SmsSendResult(True, message_sid=r.json().get("sid"))
    body_text = r.text[:300] if r.text else ""
    return SmsSendResult(False, error=body_text, status_code=r.status_code)


async def configure_sms_webhook(
    creds: TwilioCredentials,
    phone_sid: str,
    sms_url: str,
) -> None:
    """Set the inbound SMS webhook URL on a Twilio number."""
    payload = {"SmsUrl": sms_url, "SmsMethod": "POST"}
    url = f"{TWILIO_BASE}/Accounts/{creds.account_sid}/IncomingPhoneNumbers/{phone_sid}.json"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, data=payload, auth=_auth(creds))
    if r.status_code not in (200, 201):
        raise TwilioError(r.status_code, r.text[:300])
