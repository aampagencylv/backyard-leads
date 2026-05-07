"""
iClosed.io Integration

Used for gating the competitor comparison report behind a scheduling action.
Prospect must book a call to see their competitive analysis.

API Docs: https://developer.iclosed.io/
Auth: Bearer token (iclosed_xxxxx)

⚠️  API KEY EXPIRES: May 2027 — set a calendar reminder to rotate it.
    Current key set: 2026-05-07
    Rotate in: Settings → API Keys → iClosed, or via runtime_config

Booking URL: https://app.iclosed.io/e/backyardmarketingpros/discovery-call
"""
from __future__ import annotations
from typing import Optional
import httpx
from dataclasses import dataclass


ICLOSED_API_BASE = "https://api.iclosed.io/v1"
ICLOSED_BOOKING_URL = "https://app.iclosed.io/e/backyardmarketingpros/discovery-call"


@dataclass
class IClosedBooking:
    success: bool = False
    booking_id: Optional[str] = None
    contact_id: Optional[str] = None
    event_time: Optional[str] = None
    error: Optional[str] = None


async def create_contact(
    api_key: str,
    first_name: str,
    last_name: str,
    email: str,
    phone: str = "",
) -> Optional[str]:
    """Create or upsert a contact in iClosed. Returns contact ID."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(
                f"{ICLOSED_API_BASE}/contacts",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "firstName": first_name,
                    "lastName": last_name,
                    "email": email,
                    "phone": phone,
                },
            )
            if r.status_code in (200, 201):
                data = r.json()
                return data.get("id") or data.get("data", {}).get("id")
        except Exception:
            pass
    return None


async def get_available_slots(
    api_key: str,
    event_slug: str = "backyardmarketingpros/discovery-call",
    days_ahead: int = 14,
) -> list:
    """Get available time slots for the next N days."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(
                f"{ICLOSED_API_BASE}/events/timeSlots",
                headers={"Authorization": f"Bearer {api_key}"},
                params={"eventSlug": event_slug, "daysAhead": days_ahead},
            )
            if r.status_code == 200:
                data = r.json()
                return data.get("data", data.get("timeSlots", []))
        except Exception:
            pass
    return []


async def book_call(
    api_key: str,
    contact_email: str,
    contact_first_name: str,
    contact_last_name: str,
    contact_phone: str = "",
    slot_time: str = "",
    event_slug: str = "backyardmarketingpros/discovery-call",
    notes: str = "",
) -> IClosedBooking:
    """Book a call via iClosed API."""
    result = IClosedBooking()

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            payload = {
                "eventSlug": event_slug,
                "inviteeEmail": contact_email,
                "inviteeFirstName": contact_first_name,
                "inviteeLastName": contact_last_name,
            }
            if contact_phone:
                payload["inviteePhone"] = contact_phone
            if slot_time:
                payload["startTime"] = slot_time
            if notes:
                payload["notes"] = notes

            r = await client.post(
                f"{ICLOSED_API_BASE}/eventCalls",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )

            if r.status_code in (200, 201):
                data = r.json()
                booking = data.get("data", data)
                result.success = True
                result.booking_id = str(booking.get("id", ""))
                result.contact_id = str(booking.get("contactId", ""))
                result.event_time = booking.get("startTime") or booking.get("scheduledAt")
            else:
                result.error = r.text[:200]

        except Exception as e:
            result.error = str(e)[:200]

    return result
