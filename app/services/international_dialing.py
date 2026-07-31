"""International dialing compliance checks.

Before initiating an outbound call to a contact, check:
1. Is calling this country enabled for the tenant?
2. Is it within the calling window for that country's timezone?
3. Does the caller have proper verification / consent for that country?

Used by voice/twiml endpoint before allowing a call.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
import zoneinfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.twilio_voice import extract_country_code_from_e164
from app.runtime_config import get_country_dialing_config, get_supported_calling_countries


@dataclass
class CallComplianceCheck:
    allowed: bool
    reason: str = ""
    warnings: list[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


async def check_call_allowed(
    db: AsyncSession,
    to_e164: str,
    tenant_id: int,
    now_utc: Optional[datetime] = None,
) -> CallComplianceCheck:
    """
    Check if an outbound call is allowed based on international compliance rules.

    Args:
        db: database session
        to_e164: recipient phone in E.164 format (e.g. "+525551234567")
        tenant_id: tenant making the call
        now_utc: current time (for testing); defaults to now

    Returns:
        CallComplianceCheck with allowed=True/False + reason/warnings
    """
    now = now_utc or datetime.now(timezone.utc)

    # Extract country from phone number
    country = extract_country_code_from_e164(to_e164)
    if not country:
        return CallComplianceCheck(
            allowed=False,
            reason=f"Could not determine country from phone number {to_e164}"
        )

    # Check if tenant is allowed to call this country.
    # Pass tenant_id EXPLICITLY: the Twilio voice webhook is unauthenticated
    # and runs on a bare async_session(), so there is no session tenant scope
    # to infer from — it resolves the user (and their tenant) from the SDK
    # identity instead. tenant_ai_config has no ORM auto-filter, so omitting
    # this either reads tenant 1's list or raises.
    supported_countries = await get_supported_calling_countries(db, tenant_id)
    # Default to US if not configured
    if not supported_countries:
        supported_countries = ["US"]

    if country not in supported_countries:
        return CallComplianceCheck(
            allowed=False,
            reason=f"Calling {country} is not enabled for your account. "
                   f"Enabled countries: {', '.join(supported_countries)}"
        )

    # Get compliance rules for the destination country
    cfg = await get_country_dialing_config(db, country)
    if not cfg:
        return CallComplianceCheck(
            allowed=False,
            reason=f"No compliance rules found for {country}. Contact support."
        )

    # Check calling window
    try:
        tz = zoneinfo.ZoneInfo(cfg["default_timezone"])
    except zoneinfo.ZoneInfoNotFoundError:
        tz = zoneinfo.ZoneInfo("America/New_York")

    local_time = now.astimezone(tz)
    hour = local_time.hour
    start = cfg["calling_window_start"]
    end = cfg["calling_window_end"]

    if not (start <= hour < end):
        return CallComplianceCheck(
            allowed=False,
            reason=f"Outside calling window for {cfg['country_name']} "
                   f"({start}am–{end}pm {tz}). It's currently {local_time.strftime('%I:%M %p')}."
        )

    # Warn about compliance requirements
    warnings = []
    if cfg["recording_consent_required"]:
        warnings.append(f"Recording consent required in {cfg['country_name']} — ensure contact has opted in")
    if cfg["caller_id_verification_required"]:
        warnings.append(f"Caller ID verification required in {cfg['country_name']} — verify number is registered")
    if cfg["requires_local_number_registration"]:
        warnings.append(f"Local number registration required in {cfg['country_name']} (e.g. COFETEL)")

    return CallComplianceCheck(allowed=True, warnings=warnings)
