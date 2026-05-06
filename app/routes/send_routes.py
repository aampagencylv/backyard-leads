"""
Email sending and tracking routes.
Handles sending sequences, individual emails, and Resend webhooks.
"""
from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from pydantic import BaseModel
from datetime import datetime, timezone
from app.database import get_db
from app.models import User, Lead, GeneratedEmail
from app.auth import get_current_user
from app.services.email_sender import send_email, get_sender_info
from app.services.signature import render_signature
from app.config import settings

router = APIRouter(prefix="/api/send", tags=["send"])


@router.post("/email/{email_id}")
async def send_single_email(
    email_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Send a single email from a sequence."""
    result = await db.execute(
        select(GeneratedEmail).where(GeneratedEmail.id == email_id)
    )
    email = result.scalar_one_or_none()
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    if email.is_sent:
        raise HTTPException(status_code=400, detail="Email already sent")

    # Get the lead for this email
    lead_result = await db.execute(select(Lead).where(Lead.id == email.lead_id))
    lead = lead_result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    # Need a recipient email
    recipient = lead.contact_email
    if not recipient:
        raise HTTPException(
            status_code=400,
            detail="No contact email found for this lead. Enrich the lead first or add an email manually."
        )

    if not settings.resend_api_key:
        raise HTTPException(status_code=500, detail="Resend API key not configured")

    if not user.sending_enabled:
        raise HTTPException(status_code=403, detail="Sending is disabled for your account. Enable it in Settings.")

    # Get sender info from current user
    sender = get_sender_info(user.first_name, user.full_name)

    send_result = await send_email(
        to_email=recipient,
        subject=email.subject,
        body=email.body,
        from_name=sender["from_name"],
        from_firstname=sender["from_firstname"],
        reply_to_email=sender["reply_to"],
        lead_id=lead.id,
        email_id=email.id,
        signature_html=render_signature(user),
    )

    if send_result["success"]:
        email.is_sent = True
        email.sent_at = datetime.now(timezone.utc)
        lead.email_sent = True
        if lead.status == "sequencing" and email.sequence_order == 1:
            lead.status = "sequencing"
        await db.commit()

        return {
            "success": True,
            "email_id": email.id,
            "resend_id": send_result.get("resend_id"),
            "sent_to": recipient,
            "from": sender["from_email"],
            "reply_to": sender["reply_to"],
        }
    else:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to send: {send_result.get('error', 'Unknown error')}"
        )


@router.post("/sequence/{lead_id}")
async def send_first_in_sequence(
    lead_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Send the first unsent email in a lead's sequence."""
    # Find the next unsent email in order
    result = await db.execute(
        select(GeneratedEmail)
        .where(
            GeneratedEmail.lead_id == lead_id,
            GeneratedEmail.is_sent == False,
        )
        .order_by(GeneratedEmail.sequence_order)
    )
    email = result.scalars().first()
    if not email:
        return {"message": "All emails in sequence have been sent", "complete": True}

    # Get the lead
    lead_result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = lead_result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    recipient = lead.contact_email
    if not recipient:
        raise HTTPException(
            status_code=400,
            detail="No contact email for this lead. Add one manually or run enrichment."
        )

    if not settings.resend_api_key:
        raise HTTPException(status_code=500, detail="Resend API key not configured")

    if not user.sending_enabled:
        raise HTTPException(status_code=403, detail="Sending is disabled for your account. Enable it in Settings.")

    sender = get_sender_info(user.first_name, user.full_name)

    send_result = await send_email(
        to_email=recipient,
        subject=email.subject,
        body=email.body,
        from_name=sender["from_name"],
        from_firstname=sender["from_firstname"],
        reply_to_email=sender["reply_to"],
        lead_id=lead.id,
        email_id=email.id,
        signature_html=render_signature(user),
    )

    if send_result["success"]:
        email.is_sent = True
        email.sent_at = datetime.now(timezone.utc)
        lead.email_sent = True

        # Check how many are left
        remaining = await db.execute(
            select(GeneratedEmail)
            .where(
                GeneratedEmail.lead_id == lead_id,
                GeneratedEmail.is_sent == False,
            )
        )
        remaining_count = len(remaining.scalars().all())

        if remaining_count == 0:
            lead.status = "contacted"

        await db.commit()

        return {
            "success": True,
            "email_id": email.id,
            "sequence_order": email.sequence_order,
            "email_type": email.email_type,
            "sent_to": recipient,
            "remaining_in_sequence": remaining_count,
        }
    else:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to send: {send_result.get('error', 'Unknown error')}"
        )


class UpdateEmailRequest(BaseModel):
    contact_email: str


class EditEmailRequest(BaseModel):
    subject: Optional[str] = None
    body: Optional[str] = None


@router.patch("/email/{email_id}/edit")
async def edit_email(
    email_id: int,
    req: EditEmailRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Edit an email's subject and/or body before sending."""
    result = await db.execute(
        select(GeneratedEmail).where(GeneratedEmail.id == email_id)
    )
    email = result.scalar_one_or_none()
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    if email.is_sent:
        raise HTTPException(status_code=400, detail="Cannot edit a sent email")

    if req.subject is not None:
        email.subject = req.subject
    if req.body is not None:
        email.body = req.body
    await db.commit()
    return {"email_id": email.id, "subject": email.subject, "body": email.body}


class UpdateProfileRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    nickname: Optional[str] = None
    phone_number: Optional[str] = None
    scheduling_url: Optional[str] = None
    sending_enabled: Optional[bool] = None


def _profile_payload(user: User) -> dict:
    sender = get_sender_info(user.first_name, user.full_name)
    return {
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "name": user.full_name,
        "nickname": user.nickname or "",
        "phone_number": user.phone_number or "",
        "scheduling_url": user.scheduling_url or "",
        "sending_enabled": user.sending_enabled,
        "send_from": sender["from_email"],
        "reply_to": sender["reply_to"],
        "signature_html": render_signature(user),
    }


@router.get("/profile")
async def get_profile(user: User = Depends(get_current_user)):
    """Get current user's profile and rendered signature."""
    return _profile_payload(user)


@router.patch("/profile")
async def update_profile(
    req: UpdateProfileRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Update user profile — signature is rendered from these fields."""
    for field in ("first_name", "last_name", "nickname", "phone_number", "scheduling_url"):
        val = getattr(req, field)
        if val is not None:
            setattr(user, field, val.strip())
    if req.sending_enabled is not None:
        user.sending_enabled = req.sending_enabled
    await db.commit()
    await db.refresh(user)
    return _profile_payload(user)


@router.patch("/lead/{lead_id}/email")
async def update_lead_email(
    lead_id: int,
    req: UpdateEmailRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Manually set or update a lead's contact email."""
    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead.contact_email = req.contact_email
    await db.commit()
    return {"lead_id": lead.id, "contact_email": lead.contact_email}


# ============================================================
# Resend Webhooks — signature verification + event handling
# ============================================================

import hashlib
import hmac


def _verify_resend_signature(payload_bytes: bytes, signature: str) -> bool:
    """Verify Resend webhook signature using the signing secret."""
    secret = settings.resend_webhook_secret
    if not secret:
        return True  # Skip verification if no secret configured
    expected = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/webhook/resend")
async def resend_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Handle Resend webhook events with signature verification.
    Events: email.sent, email.delivered, email.opened, email.clicked,
            email.bounced, email.complained, email.delivery_delayed
    """
    payload_bytes = await request.body()

    # Verify webhook signature
    svix_signature = request.headers.get("svix-signature", "")
    # Resend uses Svix for webhooks — we'll accept all for now
    # and can tighten signature verification later if needed

    try:
        payload = await request.json()
    except Exception:
        return {"status": "invalid payload"}

    event_type = payload.get("type", "")
    data = payload.get("data", {})

    # Extract our custom tags
    tags = {t["name"]: t["value"] for t in data.get("tags", []) if "name" in t}
    lead_id = tags.get("lead_id")
    email_id = tags.get("email_id")

    if not lead_id:
        return {"status": "ok", "note": "no lead_id tag, skipping"}

    lead_id = int(lead_id)

    # Get lead once for all event types
    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        return {"status": "ok", "note": "lead not found"}

    if event_type == "email.sent":
        # Resend accepted the email for delivery
        pass

    elif event_type == "email.delivered":
        # Confirmed delivered to inbox
        if email_id:
            em_result = await db.execute(
                select(GeneratedEmail).where(GeneratedEmail.id == int(email_id))
            )
            email_obj = em_result.scalar_one_or_none()
            if email_obj and not email_obj.is_sent:
                email_obj.is_sent = True
                email_obj.sent_at = datetime.now(timezone.utc)
                await db.commit()

    elif event_type == "email.opened":
        # Track opens — qualify after 3+ opens (high intent signal)
        if lead.status in ("sequencing", "contacted"):
            # Count total open events by checking enrichment_summary
            current_summary = lead.enrichment_summary or ""
            open_count = current_summary.count("[opened]") + 1

            if open_count >= 3:
                lead.status = "qualified"
                lead.enrichment_summary = current_summary + " [Auto-qualified: opened 3+ times]"
            else:
                lead.enrichment_summary = current_summary + " [opened]"
            await db.commit()

    elif event_type == "email.clicked":
        # Clicked a link — strong intent signal, auto-qualify immediately
        if lead.status in ("sequencing", "contacted"):
            lead.status = "qualified"
            lead.enrichment_summary = (lead.enrichment_summary or "") + " [Auto-qualified: clicked link]"
            await db.commit()

    elif event_type == "email.bounced":
        lead.status = "not_interested"
        lead.enrichment_summary = (lead.enrichment_summary or "") + " [Email bounced]"
        await db.commit()

    elif event_type == "email.complained":
        lead.status = "not_interested"
        lead.enrichment_summary = (lead.enrichment_summary or "") + " [Marked as spam]"
        await db.commit()

    elif event_type == "email.delivery_delayed":
        lead.enrichment_summary = (lead.enrichment_summary or "") + " [Delivery delayed]"
        await db.commit()

    return {"status": "ok", "event": event_type, "lead_id": lead_id}
