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


# ============================================================
# Phase 2: Voice SDK access tokens + outbound TwiML
# ============================================================

def generate_access_token(creds: TwilioCredentials, identity: str, ttl_seconds: int = 3600) -> str:
    """
    Mint a JWT access token for the Twilio Voice JavaScript SDK.
    Token includes a VoiceGrant tied to the TwiML App SID, so when the
    SDK initiates a call it'll hit our /voice/twiml endpoint.

    Identity uniquely identifies this rep's browser session — we use
    'bmp_user_{id}' (set at user creation, see migrate_twilio_fields).
    """
    if not creds.is_voice_sdk_ready:
        raise ValueError(
            "Twilio not fully configured for SDK: need account_sid + auth_token + "
            "api_key_sid + api_key_secret + twiml_app_sid"
        )

    # Lazy import so the module loads even if twilio package isn't installed locally
    from twilio.jwt.access_token import AccessToken
    from twilio.jwt.access_token.grants import VoiceGrant

    token = AccessToken(
        creds.account_sid,
        creds.api_key_sid,
        creds.api_key_secret,
        identity=identity,
        ttl=ttl_seconds,
    )
    voice_grant = VoiceGrant(
        outgoing_application_sid=creds.twiml_app_sid,
        incoming_allow=True,  # so the same identity can RECEIVE inbound calls (Phase 4)
    )
    token.add_grant(voice_grant)
    return token.to_jwt()


def build_outbound_twiml(
    to_number: str,
    caller_id: str,
    record_calls: bool = True,
    recording_status_callback: Optional[str] = None,
    consent_disclosure: bool = True,
) -> str:
    """
    Build the TwiML response for an outbound call initiated via the Voice SDK.

    record_calls — if True, both legs are recorded ('record-from-answer-dual').
    consent_disclosure — if True, plays a brief 2-party-consent message before
      connecting. Required in NV, CA, FL, IL, MD, MA, MT, NH, PA, WA, CT, DE.
    """
    from twilio.twiml.voice_response import VoiceResponse, Dial

    response = VoiceResponse()
    if consent_disclosure:
        response.say(
            "This call may be recorded for quality and training purposes.",
            voice="Polly.Joanna-Neural",
        )

    dial = Dial(caller_id=caller_id)
    if record_calls:
        dial.record = "record-from-answer-dual"
        if recording_status_callback:
            dial.recording_status_callback = recording_status_callback
            dial.recording_status_callback_event = "completed"
    dial.number(to_number)
    response.append(dial)
    return str(response)


def parse_inbound_twiml(from_number: str, dial_to_user_identity: str) -> str:
    """
    TwiML for INBOUND calls — ring the rep's browser via their identity.
    If they don't pick up, fall through to voicemail (Phase 4).
    """
    from twilio.twiml.voice_response import VoiceResponse, Dial

    response = VoiceResponse()
    dial = Dial(timeout=20, action="/api/twilio/voice/inbound-fallback")
    dial.client(dial_to_user_identity)
    response.append(dial)
    return str(response)
