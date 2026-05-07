"""
Blooio messaging service — iMessage primary, capability check, inbound webhook.

Why Blooio: iMessage gets 3-4× higher response rates than SMS for B2B
cold outreach in iPhone-heavy markets. No A2P 10DLC compliance burden.
The dedicated Blooio number sends from a single business identity that
shows up in the recipient's "Backyard Marketing Pros" contact thread.

Per-rep numbers aren't supported by Blooio's iMessage type — all sends
go through the org's one dedicated number. Per-rep attribution lives
in the CRM (Activity.user_id), invisible to the recipient.

API surface used:
  GET  /v2/api/me/numbers              — verify key + show assigned number
  POST /v2/api/chats/{chatId}/messages — send (chatId = E.164 phone, e.g. +13055551234)
  GET  /v2/api/phone-numbers/lookup    — capability check (Enterprise plan only)
  POST /v2/api/webhooks                — register inbound URL (one-time setup)

Inbound webhook events handled:
  message.received  — log to timeline, auto-pause email sequence
  message.delivered — update Activity status
  message.failed    — update Activity + flag for retry
  message.read      — log a read activity (Activity type='imessage_read')
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List
import httpx

from app.services.twilio_voice import normalize_phone_e164


BLOOIO_BASE = "https://backend.blooio.com/v2/api"


class BlooioError(Exception):
    def __init__(self, status: int, message: str, body: dict | None = None):
        super().__init__(f"Blooio {status}: {message}")
        self.status = status
        self.body = body or {}


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


# ============================================================
# Connection test (Settings UI Test button)
# ============================================================

@dataclass
class BlooioAccount:
    organization_name: Optional[str] = None
    organization_id: Optional[str] = None
    key_tag: Optional[str] = None  # metadata.name on the key — Blooio dashboard label (e.g. "BYMP" vs "GHL")
    numbers: List[str] = None
    primary_number: Optional[str] = None
    error: Optional[str] = None


async def test_connection(api_key: str) -> BlooioAccount:
    """GET /me — verifies key auth + returns org info + key tag.

    /me is the canonical "who am I" call. /me/numbers exists too but on
    most plans returns an empty list (numbers are visible in the Blooio
    dashboard, not via API), so we don't surface a primary number from
    there — the org name + key tag are the meaningful confirmation.
    """
    if not api_key:
        return BlooioAccount(numbers=[], error="No API key configured")
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(f"{BLOOIO_BASE}/me", headers=_headers(api_key))
        except httpx.HTTPError as e:
            return BlooioAccount(numbers=[], error=f"Network error: {e}")
    if r.status_code != 200:
        return BlooioAccount(numbers=[], error=f"{r.status_code}: {r.text[:200]}")
    body = r.json() or {}
    org = body.get("organization") or {}
    meta = body.get("metadata") or {}
    # Best-effort number lookup — empty for most plans, harmless when it is
    nums: List[str] = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r2 = await client.get(f"{BLOOIO_BASE}/me/numbers", headers=_headers(api_key))
        if r2.status_code == 200:
            raw = (r2.json() or {}).get("numbers") or []
            for item in raw if isinstance(raw, list) else []:
                if isinstance(item, str):
                    nums.append(item)
                elif isinstance(item, dict):
                    n = item.get("number") or item.get("phone_number") or item.get("e164")
                    if n:
                        nums.append(n)
    except httpx.HTTPError:
        pass
    return BlooioAccount(
        organization_name=org.get("name"),
        organization_id=org.get("organization_id") or body.get("organization_id"),
        key_tag=meta.get("name"),
        numbers=nums,
        primary_number=nums[0] if nums else None,
    )


# ============================================================
# Send message
# ============================================================

@dataclass
class BlooioSendResult:
    success: bool
    message_id: Optional[str] = None
    chat_id: Optional[str] = None
    channel: Optional[str] = None  # 'imessage' | 'sms' (whatever Blooio routed via)
    error: Optional[str] = None
    status_code: Optional[int] = None


async def send_message(
    api_key: str,
    to_phone: str,
    text: str,
    from_number: Optional[str] = None,
    share_contact: bool = False,
    use_typing_indicator: bool = True,
) -> BlooioSendResult:
    """
    Send an iMessage to a phone number.
    chatId = E.164 phone (Blooio creates the chat if needed, reuses if exists).
    """
    to_e164 = normalize_phone_e164(to_phone)
    if not to_e164:
        return BlooioSendResult(False, error=f"Invalid phone: {to_phone}")
    if not api_key:
        return BlooioSendResult(False, error="No Blooio API key configured")

    payload = {
        "text": text,
        "use_typing_indicator": use_typing_indicator,
        "share_contact": share_contact,
    }
    if from_number:
        payload["from_number"] = from_number

    # chatId in the URL path — must be URL-safe. Blooio accepts E.164 directly.
    from urllib.parse import quote
    chat_id = quote(to_e164, safe="")
    url = f"{BLOOIO_BASE}/chats/{chat_id}/messages"

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.post(url, headers=_headers(api_key), json=payload)
        except httpx.HTTPError as e:
            return BlooioSendResult(False, error=f"Network error: {e}")

    if r.status_code in (200, 201):
        body = r.json() or {}
        # Response shape varies; pull a message id and chat id where we can find them
        message_id = body.get("id") or body.get("message_id") or body.get("messageId")
        if not message_id and isinstance(body.get("data"), dict):
            message_id = body["data"].get("id") or body["data"].get("message_id")
        return BlooioSendResult(
            success=True,
            message_id=message_id,
            chat_id=to_e164,
            channel=body.get("channel") or "imessage",
        )
    return BlooioSendResult(
        success=False,
        error=r.text[:300],
        status_code=r.status_code,
    )


# ============================================================
# Capability lookup (Enterprise plan only — not all keys can call this)
# ============================================================

@dataclass
class BlooioCapability:
    imessage: bool = False
    sms: bool = False
    available: bool = False  # at least one channel
    error: Optional[str] = None


async def check_capability(api_key: str, phone: str) -> BlooioCapability:
    """
    GET /phone-numbers/lookup?number=...
    Returns whether iMessage / SMS is available for the recipient.
    NOTE: Per Blooio docs this endpoint requires an Enterprise plan and
    returns 403 otherwise. We treat 403 as "unknown — assume iMessage is
    worth trying."
    """
    e164 = normalize_phone_e164(phone)
    if not e164:
        return BlooioCapability(error=f"Invalid phone: {phone}")
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(
                f"{BLOOIO_BASE}/phone-numbers/lookup",
                params={"number": e164},
                headers=_headers(api_key),
            )
        except httpx.HTTPError as e:
            return BlooioCapability(error=f"Network error: {e}")
    if r.status_code == 403:
        # Not Enterprise — assume iMessage available (Blooio will fail loudly
        # if it isn't). Better than blocking the send entirely.
        return BlooioCapability(imessage=True, available=True)
    if r.status_code != 200:
        return BlooioCapability(error=f"{r.status_code}: {r.text[:200]}")
    body = r.json() or {}
    if isinstance(body.get("data"), dict):
        body = body["data"]
    has_imessage = bool(body.get("imessage"))
    has_sms = bool(body.get("sms"))
    return BlooioCapability(
        imessage=has_imessage,
        sms=has_sms,
        available=has_imessage or has_sms,
    )
