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


def normalize_phone_e164(raw: str | None, default_country: str = "1") -> str:
    """
    Normalize a phone number to E.164 format that Twilio can dial.
    Accepts inputs like:
        '480-338-3369'      → '+14803383369'
        '(480) 338-3369'    → '+14803383369'
        '+1 (480) 338 3369' → '+14803383369'
        '14803383369'       → '+14803383369'
        '4803383369'        → '+14803383369'  (assumes default_country='1' for US)
    Returns '' for invalid input.
    """
    if not raw:
        return ""
    # Keep digits + leading '+' only
    s = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    if not s:
        return ""
    if s.startswith("+"):
        return s
    # No country code prefix — assume default
    if len(s) == 10:
        return f"+{default_country}{s}"
    if len(s) == 11 and s.startswith(default_country):
        return f"+{s}"
    # Anything else, just prepend '+' and hope it's valid
    return f"+{s}"


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


def parse_inbound_twiml(
    rep_identity: str,
    rep_personal_phone: Optional[str],
    voicemail_action_url: str,
    timeout: int = 20,
) -> str:
    """
    TwiML for INBOUND calls — ring the rep's browser AND optionally their
    personal phone simultaneously (whichever picks up first wins). If no
    answer in `timeout` seconds, fall through to voicemail.
    """
    from twilio.twiml.voice_response import VoiceResponse, Dial

    response = VoiceResponse()
    dial = Dial(timeout=timeout, action=voicemail_action_url, method="POST")
    if rep_identity:
        dial.client(rep_identity)
    if rep_personal_phone:
        dial.number(rep_personal_phone)
    response.append(dial)
    return str(response)


def build_voicemail_twiml(
    company_name: str,
    rep_first_name: Optional[str] = None,
    recording_status_callback: Optional[str] = None,
) -> str:
    """TwiML that plays a greeting + records a voicemail."""
    from twilio.twiml.voice_response import VoiceResponse

    response = VoiceResponse()
    greeting = (
        f"You've reached {rep_first_name} at {company_name}. "
        if rep_first_name else
        f"Thanks for calling {company_name}. "
    )
    response.say(
        greeting + "Please leave a message after the tone and we'll get back to you shortly.",
        voice="Polly.Joanna-Neural",
    )
    record_kwargs = dict(
        max_length=120,
        play_beep=True,
        transcribe=False,  # we run our own pipeline (Deepgram)
        finish_on_key="#",
    )
    if recording_status_callback:
        record_kwargs["recording_status_callback"] = recording_status_callback
        record_kwargs["recording_status_callback_event"] = "completed"
    response.record(**record_kwargs)
    response.hangup()
    return str(response)


def build_bridge_twiml(
    prospect_number: str,
    caller_id: str,
    record_calls: bool = True,
    recording_status_callback: Optional[str] = None,
    consent_disclosure: bool = True,
) -> str:
    """
    TwiML used when the rep's PERSONAL phone has answered our outbound
    bridge call. Connects them to the prospect, records both legs.
    """
    from twilio.twiml.voice_response import VoiceResponse, Dial

    response = VoiceResponse()
    if consent_disclosure:
        response.say(
            "Connecting your call now. This call may be recorded.",
            voice="Polly.Joanna-Neural",
        )
    dial = Dial(caller_id=caller_id, timeout=30)
    if record_calls:
        dial.record = "record-from-answer-dual"
        if recording_status_callback:
            dial.recording_status_callback = recording_status_callback
            dial.recording_status_callback_event = "completed"
    dial.number(prospect_number)
    response.append(dial)
    return str(response)


async def initiate_bridge_call(
    creds: TwilioCredentials,
    rep_personal_phone: str,
    rep_caller_id: str,
    bridge_twiml_url: str,
    status_callback_url: str,
) -> str:
    """
    Place an outbound call FROM Twilio TO the rep's personal phone.
    When the rep picks up, Twilio fetches `bridge_twiml_url` to know who
    to connect them to. Returns the parent CallSid.

    The caller_id shown to the rep on their personal phone will be
    `rep_caller_id` (their own assigned Twilio number) so they can
    distinguish it from spam.
    """
    if not creds.is_minimally_configured:
        raise TwilioError(400, "Twilio not configured")

    payload = {
        "To":   rep_personal_phone,
        "From": rep_caller_id,
        "Url":  bridge_twiml_url,
        "StatusCallback": status_callback_url,
        "StatusCallbackEvent": "initiated ringing answered completed",
        "StatusCallbackMethod": "POST",
    }
    url = f"{TWILIO_BASE}/Accounts/{creds.account_sid}/Calls.json"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, data=payload, auth=_auth(creds))
    if r.status_code not in (200, 201):
        raise TwilioError(r.status_code, r.text[:300])
    return r.json().get("sid", "")


async def configure_inbound_voice_url(
    creds: TwilioCredentials,
    phone_sid: str,
    voice_url: str,
    voice_method: str = "POST",
    status_callback: Optional[str] = None,
) -> None:
    """Update an owned Twilio number's inbound voice webhook URL.
    Called after a number is bought + assigned to a rep so inbound calls
    actually route somewhere.
    """
    payload = {"VoiceUrl": voice_url, "VoiceMethod": voice_method}
    if status_callback:
        payload["StatusCallback"] = status_callback
        payload["StatusCallbackMethod"] = "POST"
    url = f"{TWILIO_BASE}/Accounts/{creds.account_sid}/IncomingPhoneNumbers/{phone_sid}.json"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, data=payload, auth=_auth(creds))
    if r.status_code not in (200, 201):
        raise TwilioError(r.status_code, r.text[:300])
