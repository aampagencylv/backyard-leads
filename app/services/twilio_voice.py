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
from urllib.parse import quote
import httpx


TWILIO_BASE = "https://api.twilio.com/2010-04-01"
TWILIO_LOOKUPS_BASE = "https://lookups.twilio.com/v2"


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
# Phone-type lookup (Twilio Lookup v2)
# ============================================================

@dataclass
class PhoneTypeResult:
    type: str  # 'mobile' | 'landline' | 'voip' | 'unknown' | 'error'
    carrier: Optional[str] = None
    error: Optional[str] = None


async def lookup_phone_type(creds: TwilioCredentials, phone: str) -> PhoneTypeResult:
    """
    GET /v2/PhoneNumbers/{e164}?Fields=line_type_intelligence

    Twilio Lookup v2 with line_type_intelligence returns the carrier type:
      - 'mobile': iMessage/SMS works
      - 'landline': can't receive SMS — refuse to send
      - 'voip': SMS may or may not work depending on the provider; treat as
        mobile for now and let Blooio fall back to RCS/SMS, which will fail
        cleanly if the VoIP carrier blocks it
      - 'fixedVoip' / 'nonFixedVoip': map to 'voip'
      - 'tollFree' / 'unknown' / etc: treat as 'unknown'

    Cost: $0.005 per lookup. We cache the result on Contact.phone_type so
    we only pay once per number.
    """
    e164 = normalize_phone_e164(phone)
    if not e164:
        return PhoneTypeResult(type="error", error=f"Invalid phone: {phone}")
    if not creds.is_minimally_configured:
        return PhoneTypeResult(type="error", error="Twilio not configured")

    url = f"{TWILIO_LOOKUPS_BASE}/PhoneNumbers/{quote(e164, safe='')}"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(url, params={"Fields": "line_type_intelligence"}, auth=_auth(creds))
        except httpx.HTTPError as e:
            return PhoneTypeResult(type="error", error=f"Network error: {e}")

    if r.status_code == 404:
        return PhoneTypeResult(type="unknown", error="Number not found by Twilio Lookup")
    if r.status_code != 200:
        return PhoneTypeResult(type="error", error=f"{r.status_code}: {r.text[:200]}")

    body = r.json() or {}
    lti = body.get("line_type_intelligence") or {}
    raw_type = (lti.get("type") or "").lower()
    carrier_name = lti.get("carrier_name") or None

    # Normalize Twilio's vocabulary down to our four buckets
    if raw_type == "mobile":
        norm = "mobile"
    elif raw_type == "landline":
        norm = "landline"
    elif raw_type in ("fixedvoip", "nonfixedvoip", "voip"):
        norm = "voip"
    else:
        norm = "unknown"

    # Credit meter — record the spend regardless of result. Idempotency key
    # uses the E.164 number so re-checks of the same number dedupe within a
    # short window (intentional — we want to know if a number is queried often).
    try:
        from app.services.credit_meter import meter_standalone as _meter_lookup
        await _meter_lookup(
            action_type="phone_lookup",
            action_ref=f"twilio_lookup:{e164}",
            metadata={"type": norm, "carrier": carrier_name},
        )
    except Exception:
        pass

    return PhoneTypeResult(type=norm, carrier=carrier_name)


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


# North American Numbering Plan: +1 is shared by the US, Canada and ~20
# Caribbean nations, so the country code alone CANNOT identify the country.
# The area code (next 3 digits) does.
#
# This matters operationally, not just cosmetically. Twilio treats each NANP
# country as its own destination with its own geographic permission, and the
# Caribbean ones are disabled by default (they are the primary target for
# international revenue-share toll fraud). Mapping every +1 to "US" meant our
# own pre-call compliance check happily approved a call to, say, the Bahamas
# because the tenant had "US" enabled — and Twilio then rejected it at the
# carrier with an error the rep couldn't act on.
#
# Only NON-US area codes are listed; anything unlisted falls through to "US".
_NANP_AREA_CODES: dict[str, str] = {
    # Caribbean + Atlantic
    "242": "BS",  # Bahamas
    "246": "BB",  # Barbados
    "264": "AI",  # Anguilla
    "268": "AG",  # Antigua and Barbuda
    "284": "VG",  # British Virgin Islands
    "340": "VI",  # U.S. Virgin Islands
    "345": "KY",  # Cayman Islands
    "441": "BM",  # Bermuda
    "473": "GD",  # Grenada
    "649": "TC",  # Turks and Caicos
    "664": "MS",  # Montserrat
    "721": "SX",  # Sint Maarten
    "758": "LC",  # Saint Lucia
    "767": "DM",  # Dominica
    "784": "VC",  # Saint Vincent and the Grenadines
    "787": "PR", "939": "PR",                      # Puerto Rico
    "809": "DO", "829": "DO", "849": "DO",         # Dominican Republic
    "868": "TT",  # Trinidad and Tobago
    "869": "KN",  # Saint Kitts and Nevis
    "876": "JM", "658": "JM",                      # Jamaica
    # Pacific territories
    "670": "MP",  # Northern Mariana Islands
    "671": "GU",  # Guam
    "684": "AS",  # American Samoa
    # Canada
    **{ac: "CA" for ac in (
        "204", "226", "236", "249", "250", "263", "289", "306", "343", "354",
        "365", "367", "368", "382", "387", "403", "416", "418", "428", "431",
        "437", "438", "450", "468", "474", "506", "514", "519", "548", "579",
        "581", "584", "587", "604", "613", "639", "647", "672", "683", "705",
        "709", "742", "753", "778", "780", "782", "807", "819", "825", "867",
        "873", "879", "902", "905",
    )},
}


def extract_country_code_from_e164(e164: str | None) -> str | None:
    """Extract the ISO country code from an E.164 phone number.
    +14803383369  → "US"
    +12425551234  → "BS"  (Bahamas — NANP, resolved by area code)
    +14165551234  → "CA"
    +5215551234   → "MX"
    +551140041234 → "BR"
    Returns None if we can't determine the country.

    Primary path is `phonenumbers` (Google's libphonenumber), which knows all
    245 dialling regions including every NANP area code — a hand-maintained
    prefix table cannot stay correct as area codes are added, and reps dial
    anywhere. The tables below remain as an offline fallback so the dialer
    degrades to "common countries still work" rather than failing shut if the
    dependency is ever missing.
    """
    if not e164 or not e164.startswith("+"):
        return None

    try:
        import phonenumbers
        parsed = phonenumbers.parse(e164, None)
        region = phonenumbers.region_code_for_number(parsed)
        # "ZZ" is libphonenumber's unknown-region sentinel.
        if region and region != "ZZ":
            return region
    except Exception:
        pass  # fall through to the static tables

    digits_only = "".join(ch for ch in e164[1:] if ch.isdigit())
    # +1 must be disambiguated by area code before the generic prefix scan,
    # otherwise every NANP number collapses to "US".
    if digits_only.startswith("1") and len(digits_only) >= 4:
        return _NANP_AREA_CODES.get(digits_only[1:4], "US")

    # Country code → ISO country code mapping (non-exhaustive but covers major countries)
    COUNTRY_CODE_MAP = {
        "1": "US",     # US / Canada / Caribbean (primary use)
        "52": "MX",    # Mexico
        "55": "BR",    # Brazil
        "57": "CO",    # Colombia
        "56": "CL",    # Chile
        "54": "AR",    # Argentina
        "51": "PE",    # Peru
        "58": "VE",    # Venezuela
        "53": "CU",    # Cuba
        "591": "BO",   # Bolivia
        "593": "EC",   # Ecuador
        "595": "PY",   # Paraguay
        "598": "UY",   # Uruguay
        # Central America. Seeded in country_dialing_config by
        # migrate_nanp_country_configs — without these prefixes the compliance
        # check fails with "could not determine country" instead of using them.
        "501": "BZ",   # Belize
        "502": "GT",   # Guatemala
        "503": "SV",   # El Salvador
        "504": "HN",   # Honduras
        "505": "NI",   # Nicaragua
        "506": "CR",   # Costa Rica
        "507": "PA",   # Panama
        "509": "HT",   # Haiti
        "34": "ES",    # Spain
        "33": "FR",    # France
        "44": "GB",    # UK
        "49": "DE",    # Germany
        "39": "IT",    # Italy
        "31": "NL",    # Netherlands
        "32": "BE",    # Belgium
        "47": "NO",    # Norway
        "46": "SE",    # Sweden
        "61": "AU",    # Australia
        "81": "JP",    # Japan
        "86": "CN",    # China
        "91": "IN",    # India
    }

    digits = e164[1:]  # Remove leading '+'
    # Try longest prefixes first (country codes are 1-3 digits)
    for prefix_len in [3, 2, 1]:
        if len(digits) >= prefix_len:
            prefix = digits[:prefix_len]
            if prefix in COUNTRY_CODE_MAP:
                return COUNTRY_CODE_MAP[prefix]

    return None


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

    dial_kwargs = {"caller_id": caller_id}
    if record_calls:
        dial_kwargs["record"] = "record-from-answer-dual"
        if recording_status_callback:
            dial_kwargs["recording_status_callback"] = recording_status_callback
            dial_kwargs["recording_status_callback_event"] = "completed"
    dial = Dial(**dial_kwargs)
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
    custom_greeting_url: Optional[str] = None,
) -> str:
    """TwiML that plays a greeting + records a voicemail.
    If custom_greeting_url is provided, plays that audio file instead of TTS."""
    from twilio.twiml.voice_response import VoiceResponse

    response = VoiceResponse()
    if custom_greeting_url:
        response.play(custom_greeting_url)
    else:
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
    dial_kwargs = {"caller_id": caller_id, "timeout": 30}
    if record_calls:
        dial_kwargs["record"] = "record-from-answer-dual"
        if recording_status_callback:
            dial_kwargs["recording_status_callback"] = recording_status_callback
            dial_kwargs["recording_status_callback_event"] = "completed"
    dial = Dial(**dial_kwargs)
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
