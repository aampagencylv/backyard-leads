"""
Blooio messaging routes — iMessage send + inbound webhook + connection test.

Phase 6 of the Twilio plan, redirected: Blooio replaces the Twilio SMS
path because iMessage gets 3-4× higher response rates and skips A2P
10DLC compliance. The dormant Twilio SMS code stays for a future
fallback channel; for now Blooio handles all "Message" actions.
"""
from __future__ import annotations
from typing import Optional
from datetime import datetime, timezone
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db, async_session
from app.models import User, Contact, Company, Activity, GeneratedEmail
from app.auth import get_current_user
from app.runtime_config import get_blooio_api_key
from app.services.blooio_messaging import (
    test_connection as blooio_test,
    send_message as blooio_send,
    check_capability,
)
from app.services.twilio_sms import is_stop_keyword, is_start_keyword

router = APIRouter(prefix="/api/blooio", tags=["blooio"])


# ============================================================
# Test connection (Settings UI)
# ============================================================

@router.get("/test")
async def test(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    api_key = await get_blooio_api_key(db)
    if not api_key:
        raise HTTPException(status_code=400, detail="No Blooio API key configured. Save one in Settings → API Keys.")
    result = await blooio_test(api_key)
    if result.error:
        raise HTTPException(status_code=502, detail=result.error)
    return {
        "success": True,
        "organization_name": result.organization_name,
        "organization_id": result.organization_id,
        "key_tag": result.key_tag,
        "numbers": result.numbers or [],
        "primary_number": result.primary_number,
    }


# ============================================================
# Send a message
# ============================================================

class SendMessageRequest(BaseModel):
    contact_id: int
    text: str


@router.post("/send")
async def send_imessage(
    req: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    contact = (await db.execute(select(Contact).where(Contact.id == req.contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if not contact.phone:
        raise HTTPException(status_code=400, detail="Contact has no phone number")
    if contact.do_not_text:
        raise HTTPException(status_code=400, detail="This contact has opted out (replied STOP).")
    if not (req.text or "").strip():
        raise HTTPException(status_code=400, detail="Message text is empty")

    api_key = await get_blooio_api_key(db)
    if not api_key:
        raise HTTPException(status_code=400, detail="Blooio not configured. Add an API key in Settings.")

    result = await blooio_send(api_key, contact.phone, req.text)
    if not result.success:
        raise HTTPException(status_code=502, detail=f"Blooio rejected: {result.error}")

    activity = Activity(
        company_id=contact.company_id,
        contact_id=contact.id,
        user_id=user.id,
        activity_type="imessage_sent",
        content=f"iMessage to {contact.full_name or contact.phone}: {req.text[:300]}{'…' if len(req.text) > 300 else ''}",
        metadata_json=json.dumps({
            "channel": result.channel or "imessage",
            "message_id": result.message_id,
            "chat_id": result.chat_id,
            "to": contact.phone,
            "text": req.text,
        }),
    )
    db.add(activity)
    await db.commit()
    await db.refresh(activity)
    return {
        "success": True,
        "message_id": result.message_id,
        "channel": result.channel,
        "activity": {
            "id": activity.id,
            "type": activity.activity_type,
            "content": activity.content,
            "created_at": activity.created_at.isoformat() if activity.created_at else None,
        },
    }


# ============================================================
# Check capability (does this contact have iMessage?)
# ============================================================

@router.get("/capability")
async def capability(
    phone: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    api_key = await get_blooio_api_key(db)
    if not api_key:
        raise HTTPException(status_code=400, detail="Blooio not configured")
    cap = await check_capability(api_key, phone)
    return {
        "imessage": cap.imessage,
        "sms": cap.sms,
        "available": cap.available,
        "error": cap.error,
    }


# ============================================================
# Inbound webhook — Blooio POSTs here when someone replies
# ============================================================

class BlooioWebhookEvent(BaseModel):
    """Loose model — Blooio's webhook payload shape varies by event type.
    We accept anything and inspect the raw JSON ourselves."""
    pass


# ============================================================
# Webhook self-registration (admin-only)
# Avoids the user having to open Blooio's console — just clicks a button.
# Coexists peacefully with the GHL webhook on the same Blooio account.
# ============================================================

WEBHOOK_FRIENDLY_NAME = "BMP Prospector — inbound iMessage handler"


@router.post("/webhook/setup")
async def webhook_setup(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Register our /api/blooio/inbound URL on Blooio. Idempotent — if
    we already have a webhook with our friendly name, we leave it alone.
    Does NOT touch other webhooks (e.g. the GHL one for the boat-test biz).
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    api_key = await get_blooio_api_key(db)
    if not api_key:
        raise HTTPException(status_code=400, detail="Blooio not configured")

    from app.config import settings as app_settings
    from app.services.blooio_messaging import BLOOIO_BASE
    import httpx

    target_url = f"{app_settings.public_url.rstrip('/')}/api/blooio/inbound"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # 1. Check existing webhooks — only register if we don't already have one
    async with httpx.AsyncClient(timeout=15) as client:
        list_r = await client.get(f"{BLOOIO_BASE}/webhooks", headers=headers)
        if list_r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Blooio list webhooks {list_r.status_code}: {list_r.text[:200]}")
        existing = list_r.json() or {}
        items = existing if isinstance(existing, list) else (existing.get("data") or existing.get("webhooks") or [])
        for w in items:
            if isinstance(w, dict) and w.get("url") == target_url:
                return {
                    "already_registered": True,
                    "webhook_id": w.get("id"),
                    "url": target_url,
                    "events": w.get("events", []),
                    "note": "BMP webhook already exists — left in place. Other webhooks (e.g. GHL) untouched.",
                }

        # 2. Register a new one
        payload = {
            "url": target_url,
            "name": WEBHOOK_FRIENDLY_NAME,
            "events": [
                "message.received",
                "message.delivered",
                "message.failed",
                "message.read",
            ],
        }
        create_r = await client.post(f"{BLOOIO_BASE}/webhooks", headers=headers, json=payload)
        if create_r.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=f"Blooio create webhook {create_r.status_code}: {create_r.text[:200]}")
        created = create_r.json() or {}
        return {
            "registered": True,
            "webhook_id": created.get("id") or (created.get("data") or {}).get("id"),
            "url": target_url,
            "events": payload["events"],
            "note": "Webhook registered. GHL webhook left untouched. Inbound from non-BMP contacts is silently ignored by our handler.",
        }


@router.post("/inbound")
async def inbound(request: Request):
    """
    Blooio webhook receiver. Handles message.received, message.delivered,
    message.failed, message.read.

    On message.received:
      - Match the From number to a known Contact
      - Log Activity type='imessage_received'
      - Auto-handle STOP/START keywords
      - Auto-pause the contact's email sequence (matches email-reply behavior)
      - Bump company status to 'replied'
    """
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=200)

    event_type = body.get("event") or body.get("type") or ""
    data = body.get("data") if isinstance(body.get("data"), dict) else body

    if event_type in ("message.received", "message_received", "imessage.received"):
        await _handle_inbound_message(data)
    elif event_type in ("message.delivered", "message.read", "message.failed"):
        await _handle_status_update(event_type, data)

    return Response(status_code=200)


async def _handle_inbound_message(data: dict) -> None:
    """Persist + react to an incoming iMessage."""
    # Extract sender + body — Blooio's payload shape varies; tolerate a few keys
    from_number = (
        data.get("from")
        or data.get("from_number")
        or data.get("sender")
        or (data.get("contact") or {}).get("phone")
        or ""
    ).strip()
    text = (
        data.get("text")
        or data.get("body")
        or data.get("message")
        or (data.get("message") or {}).get("text") if isinstance(data.get("message"), dict) else ""
        or ""
    )
    if isinstance(text, list):
        text = " ".join(str(t) for t in text)
    text = (text or "").strip()
    message_id = data.get("id") or data.get("message_id") or data.get("messageId") or ""

    if not from_number or not text:
        return

    async with async_session() as db:
        contact = (await db.execute(
            select(Contact).where(Contact.phone == from_number)
        )).scalar_one_or_none()
        if not contact:
            # Unknown sender — log nothing for now (could create placeholder later).
            return

        # STOP / opt-out
        if is_stop_keyword(text):
            contact.do_not_text = True
            contact.do_not_text_at = datetime.now(timezone.utc)
            db.add(Activity(
                company_id=contact.company_id, contact_id=contact.id,
                activity_type="sms_opt_out",
                content=f"iMessage opt-out (STOP) from {contact.full_name or from_number}",
                metadata_json=json.dumps({"channel": "imessage", "message_id": message_id, "body": text}),
            ))
            await _pause_email_sequence(db, contact)
            await db.commit()
            return

        # START / opt back in
        if is_start_keyword(text) and contact.do_not_text:
            contact.do_not_text = False
            contact.do_not_text_at = None
            db.add(Activity(
                company_id=contact.company_id, contact_id=contact.id,
                activity_type="sms_opt_in",
                content=f"iMessage opt-in restored (START) from {contact.full_name or from_number}",
            ))
            await db.commit()
            return

        # Regular reply
        db.add(Activity(
            company_id=contact.company_id,
            contact_id=contact.id,
            activity_type="imessage_received",
            content=f"iMessage from {contact.full_name or from_number}: {text[:300]}{'…' if len(text) > 300 else ''}",
            metadata_json=json.dumps({
                "channel": "imessage",
                "message_id": message_id,
                "from": from_number,
                "text": text,
            }),
        ))

        # Auto-pause email sequence (parallel to email-reply / SMS-reply behavior)
        await _pause_email_sequence(db, contact, channel_label="iMessage")

        # Bump status if this is the first response
        company = (await db.execute(select(Company).where(Company.id == contact.company_id))).scalar_one_or_none()
        if company and company.status in ("sequencing", "contacted"):
            company.status = "replied"

        await db.commit()


async def _pause_email_sequence(db: AsyncSession, contact: Contact, channel_label: str = "iMessage") -> None:
    pending = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.contact_id == contact.id,
            GeneratedEmail.is_sent == False,
            GeneratedEmail.paused_at.is_(None),
        )
    )).scalars().all()
    if not pending:
        return
    now = datetime.now(timezone.utc)
    for e in pending:
        e.paused_at = now
    db.add(Activity(
        company_id=contact.company_id, contact_id=contact.id,
        activity_type="sequence_paused",
        content=f"Email sequence auto-paused — contact replied via {channel_label} ({len(pending)} emails)",
    ))


async def _handle_status_update(event_type: str, data: dict) -> None:
    """Update an existing Activity when delivery / read / failed status arrives."""
    message_id = data.get("id") or data.get("message_id") or data.get("messageId") or ""
    if not message_id:
        return
    async with async_session() as db:
        # Find the matching imessage_sent Activity by message_id in metadata_json
        rows = (await db.execute(
            select(Activity).where(
                Activity.activity_type == "imessage_sent",
                Activity.metadata_json.like(f'%"{message_id}"%'),
            )
        )).scalars().all()
        for a in rows:
            meta = json.loads(a.metadata_json) if a.metadata_json else {}
            if event_type in ("message.delivered", "message_delivered"):
                meta["delivery_status"] = "delivered"
            elif event_type in ("message.read", "message_read"):
                meta["delivery_status"] = "read"
            elif event_type in ("message.failed", "message_failed"):
                meta["delivery_status"] = "failed"
                meta["failure_reason"] = data.get("error") or data.get("reason") or ""
                a.content = a.content + " [DELIVERY FAILED]"
            a.metadata_json = json.dumps(meta)
        await db.commit()
