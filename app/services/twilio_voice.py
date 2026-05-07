"""
Twilio Voice integration — Phase 1: number management only.

Phase 2 (click-to-call) and beyond will add:
  - generate_voice_sdk_token(user)
  - twiml_for_outbound_call(...)
  - status callback handlers
  - recording webhook handler
  - Whisper transcription dispatch
  - Claude call-summary generation

This module exposes admin-tier helpers for buying / listing / assigning /
releasing Twilio phone numbers via the Twilio REST API.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional
import httpx


TWILIO_BASE = "https://api.twilio.com/2010-04-01"


@dataclass
class TwilioNumber:
    """Available-for-purchase or already-owned Twilio phone number."""
    phone_number: str           # E.164 e.g. "+17025551234"
    friendly_name: Optional[str] = None
    locality: Optional[str] = None
    region: Optional[str] = None  # state code
    iso_country: Optional[str] = None
    sid: Optional[str] = None     # PNxxxx for owned numbers
    capabilities: dict = None     # {"voice": True, "SMS": True, "MMS": False}


@dataclass
class TwilioCredentials:
    account_sid: str
    auth_token: str
    api_key_sid: Optional[str] = None
    api_key_secret: Optional[str] = None
    twiml_app_sid: Optional[str] = None

    @property
    def is_minimally_configured(self) -> bool:
        return bool(self.account_sid and self.auth_token)

    @property
    def is_voice_sdk_ready(self) -> bool:
        """Phase 2 needs API Key/Secret + TwiML App SID for browser SDK auth."""
        return bool(
            self.account_sid and self.auth_token
            and self.api_key_sid and self.api_key_secret and self.twiml_app_sid
        )


class TwilioError(Exception):
    """Raised on non-success Twilio API responses. Includes status + parsed body."""
    def __init__(self, status: int, message: str, body: dict | None = None):
        super().__init__(f"Twilio {status}: {message}")
        self.status = status
        self.body = body or {}


def _auth(creds: TwilioCredentials) -> tuple[str, str]:
    return (creds.account_sid, creds.auth_token)


async def search_available_numbers(
    creds: TwilioCredentials,
    area_code: Optional[str] = None,
    contains: Optional[str] = None,
    iso_country: str = "US",
    limit: int = 10,
) -> list[TwilioNumber]:
    """
    Search Twilio's inventory for numbers available to purchase.
    Filter by area code (e.g. '702' for Vegas), or 'contains' digit pattern.
    """
    params: dict[str, object] = {"PageSize": min(limit, 30), "VoiceEnabled": "true"}
    if area_code:
        params["AreaCode"] = area_code
    if contains:
        params["Contains"] = contains

    url = f"{TWILIO_BASE}/Accounts/{creds.account_sid}/AvailablePhoneNumbers/{iso_country}/Local.json"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params, auth=_auth(creds))
    if r.status_code != 200:
        raise TwilioError(r.status_code, r.text[:300])
    data = r.json()
    out: list[TwilioNumber] = []
    for n in data.get("available_phone_numbers", [])[:limit]:
        out.append(TwilioNumber(
            phone_number=n.get("phone_number", ""),
            friendly_name=n.get("friendly_name"),
            locality=n.get("locality"),
            region=n.get("region"),
            iso_country=n.get("iso_country"),
            capabilities=n.get("capabilities") or {},
        ))
    return out


async def buy_number(
    creds: TwilioCredentials,
    phone_number: str,
    voice_url: Optional[str] = None,
    status_callback: Optional[str] = None,
) -> TwilioNumber:
    """
    Provision an available number into the account.
    voice_url = TwiML Bin or webhook that handles inbound calls (set in Phase 4).
    """
    payload: dict[str, str] = {"PhoneNumber": phone_number}
    if voice_url:
        payload["VoiceUrl"] = voice_url
    if status_callback:
        payload["StatusCallback"] = status_callback

    url = f"{TWILIO_BASE}/Accounts/{creds.account_sid}/IncomingPhoneNumbers.json"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, data=payload, auth=_auth(creds))
    if r.status_code not in (200, 201):
        raise TwilioError(r.status_code, r.text[:300])
    n = r.json()
    return TwilioNumber(
        phone_number=n.get("phone_number", ""),
        friendly_name=n.get("friendly_name"),
        sid=n.get("sid"),
        capabilities=n.get("capabilities") or {},
    )


async def list_owned_numbers(creds: TwilioCredentials) -> list[TwilioNumber]:
    """List all phone numbers we own under this Twilio account."""
    url = f"{TWILIO_BASE}/Accounts/{creds.account_sid}/IncomingPhoneNumbers.json"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params={"PageSize": 100}, auth=_auth(creds))
    if r.status_code != 200:
        raise TwilioError(r.status_code, r.text[:300])
    data = r.json()
    return [
        TwilioNumber(
            phone_number=n.get("phone_number", ""),
            friendly_name=n.get("friendly_name"),
            sid=n.get("sid"),
            capabilities=n.get("capabilities") or {},
        )
        for n in data.get("incoming_phone_numbers", [])
    ]


async def release_number(creds: TwilioCredentials, phone_sid: str) -> None:
    """Release a number back to Twilio (stops $1.15/mo billing)."""
    url = f"{TWILIO_BASE}/Accounts/{creds.account_sid}/IncomingPhoneNumbers/{phone_sid}.json"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.delete(url, auth=_auth(creds))
    if r.status_code not in (200, 204):
        raise TwilioError(r.status_code, r.text[:300])


def number_to_dict(n: TwilioNumber) -> dict:
    return asdict(n)
