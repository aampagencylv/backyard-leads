"""
Email sending and tracking routes.
Send individual emails, send next-in-sequence for a contact, edit before send,
handle Resend webhook events (auto-pause sequence on reply, auto-qualify on click/3+opens).
"""
from __future__ import annotations
from typing import Optional
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
import json
from pydantic import BaseModel

from app.tenancy import get_tenant_db
from app.models import User, Company, Contact, GeneratedEmail, Activity, Task, Deal
from app.auth import get_current_user
from app.services.email_sender import send_email, get_sender_info
from app.services.signature import render_signature
from app.config import settings

router = APIRouter(prefix="/api/send", tags=["send"])


# ============================================================
# Send: single email
# ============================================================

@router.post("/email/{email_id}")
async def send_single_email(
    email_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    email = (await db.execute(select(GeneratedEmail).where(GeneratedEmail.id == email_id))).scalar_one_or_none()
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    if email.is_sent:
        raise HTTPException(status_code=400, detail="Email already sent")
    if email.paused_at:
        raise HTTPException(status_code=400, detail="Sequence is paused (the contact has replied or unsubscribed). Resume from the contact card to send.")
    # Hard gate: only email step_types can be dispatched through this route.
    # Without this check, a BDR clicking Send on a call/linkedin/imessage row
    # would silently send the talk-track or chat draft to the prospect's email
    # inbox with a placeholder subject ('Call 3', 'LinkedIn step 2'). Found
    # 2026-06-03 after Sebastian sent a call talk-track to texasremodelteam.com
    # with subject 'Call 3' (the placeholder), and the prospect opened it.
    if (email.step_type or "email") != "email":
        raise HTTPException(
            status_code=400,
            detail=(
                f"This step is a {(email.step_type or '').upper()} task, not an email. "
                "Complete it from the Tasks panel — clicking Send here would have "
                "emailed the talk-track / chat draft to the prospect."
            ),
        )
    if email.skipped_at:
        raise HTTPException(
            status_code=400,
            detail=f"This step was skipped at creation ({email.skip_reason or 'no reason'}). It has placeholder copy and should not be sent.",
        )

    contact = (await db.execute(select(Contact).where(Contact.id == email.contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found for this email")
    if not contact.email:
        raise HTTPException(status_code=400, detail="Contact has no email address. Add one first.")
    if contact.unsubscribed_at:
        raise HTTPException(status_code=400, detail="Contact has unsubscribed.")
    # Hard gate: verify the contact's email before send. Caches on
    # contact.email_status so subsequent sends skip the verify cost.
    from app.services.email_validation import ensure_email_validated
    ok_to_send, gate_reason = await ensure_email_validated(db, contact)
    if not ok_to_send:
        raise HTTPException(status_code=400, detail=f"Email failed verification ({gate_reason}). Update the email or remove the contact.")

    company = (await db.execute(select(Company).where(Company.id == email.company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    if not settings.resend_api_key:
        raise HTTPException(status_code=500, detail="Email service not configured")
    if not user.sending_enabled:
        raise HTTPException(status_code=403, detail="Sending is disabled for your account. Enable it in Settings.")

    sender = get_sender_info(user.first_name, user.full_name)
    # Token-based Reply-To so prospect replies route through our inbound webhook
    from app.services.email_sender import generate_reply_token, reply_to_for_token
    if not email.reply_token:
        email.reply_token = generate_reply_token()
    sender["reply_to"] = reply_to_for_token(email.reply_token)
    from app.services.tracking import wrap_html_links
    tracked_body = await wrap_html_links(
        db, email.body, contact_id=contact.id, company_id=company.id, email_id=email.id, label="body_link",
    )
    sig_html = await render_signature(db, user)
    tracked_signature = await wrap_html_links(
        db, sig_html, contact_id=contact.id, company_id=company.id, email_id=email.id, label="signature_link",
    )
    result = await send_email(
        to_email=contact.email,
        subject=email.subject,
        body=tracked_body,
        from_name=sender["from_name"],
        from_firstname=sender["from_firstname"],
        reply_to_email=sender["reply_to"],
        company_id=company.id,
        contact_id=contact.id,
        email_id=email.id,
        signature_html=tracked_signature,
        unsubscribe_token=contact.unsubscribe_token,
    )

    if result["success"]:
        email.is_sent = True
        email.sent_at = datetime.now(timezone.utc)
        email.sent_by_user_id = user.id
        company.email_sent = True
        if company.status not in ("contacted", "replied", "qualified", "converted"):
            company.status = "sequencing"
        db.add(Activity(company_id=company.id, contact_id=contact.id, user_id=user.id,
                        activity_type="email_sent",
                        content=f"Sent: {email.subject}"))
        from app.services.credit_meter import meter, make_idem_key
        await meter(
            db, action_type="email_send",
            idempotency_key=make_idem_key("email_send", email.id),
            user_id=user.id, action_ref=f"generated_email:{email.id}",
        )
        await db.commit()
        return {"success": True, "email_id": email.id, "resend_id": result.get("resend_id"),
                "sent_to": contact.email, "from": sender["from_email"], "reply_to": sender["reply_to"]}
    # Failure path. If Resend reported a transient failure (timeout, 5xx,
    # network blip), surface 503 + Retry-After so callers retry without
    # treating the email as permanently failed. The step stays unsent —
    # engine's next tick re-attempts. Previously raised httpx.ReadTimeout
    # escaped all the way to Sentry (incident 970c574 from 2026-06-03).
    if result.get("retryable"):
        raise HTTPException(
            status_code=503,
            detail=f"Resend transient — retry queued: {result.get('error', '')}",
            headers={"Retry-After": "60"},
        )
    raise HTTPException(status_code=500, detail=f"Failed to send: {result.get('error', 'Unknown error')}")


# ============================================================
# Send: next-in-sequence for a contact
# ============================================================

@router.post("/contact/{contact_id}/sequence")
async def send_next_in_sequence(
    contact_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Send the next unsent (and unpaused) email in this contact's sequence."""
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if contact.unsubscribed_at:
        raise HTTPException(status_code=400, detail="Contact has unsubscribed.")
    if not contact.email:
        raise HTTPException(status_code=400, detail="Contact has no email address.")

    # Filter by step_type='email' so we never accidentally send a
    # call-talk-track or LinkedIn DM draft to the prospect's INBOX.
    # Also filter out skipped rows (placeholder subject/body). This route
    # is "send the next EMAIL" — call/linkedin steps are completed via the
    # Tasks panel, not via Send Email. The Texas Remodel Team incident on
    # 2026-06-03 was caused by the missing step_type filter here.
    email = (await db.execute(
        select(GeneratedEmail)
        .where(
            GeneratedEmail.contact_id == contact_id,
            GeneratedEmail.is_sent == False,
            GeneratedEmail.paused_at.is_(None),
            GeneratedEmail.skipped_at.is_(None),
            GeneratedEmail.step_type == "email",
        )
        .order_by(GeneratedEmail.sequence_order)
    )).scalars().first()

    if not email:
        return {"message": "All emails in sequence have been sent (or sequence is paused)", "complete": True}

    company = (await db.execute(select(Company).where(Company.id == email.company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found for this email")
    if not settings.resend_api_key:
        raise HTTPException(status_code=500, detail="Email service not configured")
    if not user.sending_enabled:
        raise HTTPException(status_code=403, detail="Sending is disabled for your account.")

    sender = get_sender_info(user.first_name, user.full_name)
    from app.services.email_sender import generate_reply_token, reply_to_for_token
    if not email.reply_token:
        email.reply_token = generate_reply_token()
    sender["reply_to"] = reply_to_for_token(email.reply_token)
    from app.services.tracking import wrap_html_links
    tracked_body = await wrap_html_links(
        db, email.body, contact_id=contact.id, company_id=company.id, email_id=email.id, label="body_link",
    )
    sig_html = await render_signature(db, user)
    tracked_signature = await wrap_html_links(
        db, sig_html, contact_id=contact.id, company_id=company.id, email_id=email.id, label="signature_link",
    )
    result = await send_email(
        to_email=contact.email,
        subject=email.subject,
        body=tracked_body,
        from_name=sender["from_name"],
        from_firstname=sender["from_firstname"],
        reply_to_email=sender["reply_to"],
        company_id=company.id,
        contact_id=contact.id,
        email_id=email.id,
        signature_html=tracked_signature,
        unsubscribe_token=contact.unsubscribe_token,
    )

    if result["success"]:
        email.is_sent = True
        email.sent_at = datetime.now(timezone.utc)
        email.sent_by_user_id = user.id
        company.email_sent = True
        from app.services.credit_meter import meter, make_idem_key
        await meter(
            db, action_type="email_send",
            idempotency_key=make_idem_key("email_send", email.id),
            user_id=user.id, action_ref=f"generated_email:{email.id}",
        )

        remaining = (await db.execute(
            select(GeneratedEmail).where(
                GeneratedEmail.contact_id == contact_id,
                GeneratedEmail.is_sent == False,
                GeneratedEmail.paused_at.is_(None),
            )
        )).scalars().all()
        if not remaining:
            company.status = "contacted"

        db.add(Activity(company_id=company.id, contact_id=contact.id, user_id=user.id,
                        activity_type="email_sent",
                        content=f"Sent: {email.subject}"))
        await db.commit()
        return {
            "success": True,
            "email_id": email.id,
            "sequence_order": email.sequence_order,
            "email_type": email.email_type,
            "sent_to": contact.email,
            "remaining_in_sequence": len(remaining),
        }
    # Failure path. If Resend reported a transient failure (timeout, 5xx,
    # network blip), surface 503 + Retry-After so callers retry without
    # treating the email as permanently failed. The step stays unsent —
    # engine's next tick re-attempts. Previously raised httpx.ReadTimeout
    # escaped all the way to Sentry (incident 970c574 from 2026-06-03).
    if result.get("retryable"):
        raise HTTPException(
            status_code=503,
            detail=f"Resend transient — retry queued: {result.get('error', '')}",
            headers={"Retry-After": "60"},
        )
    raise HTTPException(status_code=500, detail=f"Failed to send: {result.get('error', 'Unknown error')}")


# ============================================================
# Edit a queued email
# ============================================================

class EditEmailRequest(BaseModel):
    subject: Optional[str] = None
    body: Optional[str] = None


@router.patch("/email/{email_id}/edit")
async def edit_email(
    email_id: int,
    req: EditEmailRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    email = (await db.execute(select(GeneratedEmail).where(GeneratedEmail.id == email_id))).scalar_one_or_none()
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


@router.delete("/email/{email_id}")
async def delete_email(
    email_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    email = (await db.execute(select(GeneratedEmail).where(GeneratedEmail.id == email_id))).scalar_one_or_none()
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    if email.is_sent:
        raise HTTPException(status_code=400, detail="Cannot delete a sent email")
    await db.delete(email)
    await db.commit()
    return {"deleted": True}


# ============================================================
# Reschedule a step
# ============================================================

class RescheduleRequest(BaseModel):
    delay_days: Optional[int] = None
    scheduled_date: Optional[str] = None  # ISO format


@router.patch("/email/{email_id}/reschedule")
async def reschedule_step(
    email_id: int,
    req: RescheduleRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Reschedule a sequence step to a different day."""
    step = (await db.execute(select(GeneratedEmail).where(GeneratedEmail.id == email_id))).scalar_one_or_none()
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")
    if step.is_sent:
        raise HTTPException(status_code=400, detail="Cannot reschedule a completed step")

    if req.scheduled_date:
        step.scheduled_send_at = datetime.fromisoformat(req.scheduled_date)
    elif req.delay_days is not None:
        step.send_delay_days = req.delay_days
        # Recalculate from sequence start
        company = (await db.execute(select(Company).where(Company.id == step.company_id))).scalar_one_or_none()
        if company and company.sequence_started_at:
            from datetime import timedelta
            step.scheduled_send_at = company.sequence_started_at + timedelta(days=req.delay_days)

    await db.commit()
    return {
        "id": step.id,
        "send_delay_days": step.send_delay_days,
        "scheduled_send_at": step.scheduled_send_at.isoformat() if step.scheduled_send_at else None,
    }


# ============================================================
# Insert a new step into a sequence
# ============================================================

class InsertStepRequest(BaseModel):
    step_type: str  # email, linkedin, call, text, custom
    subject: str
    body: str
    delay_days: int = 0
    after_order: Optional[int] = None  # Insert after this sequence_order; None = append at end


@router.post("/contact/{contact_id}/add-step")
async def add_sequence_step(
    contact_id: int,
    req: InsertStepRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Add a custom step to a contact's sequence (email, LinkedIn, call, text, or custom task)."""
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    company = (await db.execute(select(Company).where(Company.id == contact.company_id))).scalar_one_or_none()

    # Get existing steps to determine order
    existing = (await db.execute(
        select(GeneratedEmail).where(GeneratedEmail.contact_id == contact_id).order_by(GeneratedEmail.sequence_order)
    )).scalars().all()

    if req.after_order is not None:
        new_order = req.after_order + 1
        # Shift all steps after this point
        for s in existing:
            if s.sequence_order >= new_order:
                s.sequence_order += 1
    else:
        new_order = (max(s.sequence_order for s in existing) + 1) if existing else 1

    scheduled_at = None
    if company and company.sequence_started_at:
        from datetime import timedelta
        scheduled_at = company.sequence_started_at + timedelta(days=req.delay_days)

    step = GeneratedEmail(
        contact_id=contact_id,
        company_id=contact.company_id,
        step_type=req.step_type,
        subject=req.subject,
        body=req.body,
        email_type=req.step_type,
        sequence_order=new_order,
        send_delay_days=req.delay_days,
        scheduled_send_at=scheduled_at,
    )
    db.add(step)

    # For non-email steps, auto-create a BDR task
    if req.step_type != "email":
        assigned_user = company.assigned_to if company else user.id
        task = Task(
            company_id=contact.company_id,
            contact_id=contact_id,
            user_id=assigned_user or user.id,
            description=f"{req.step_type.title()}: {req.subject}",
            due_date=scheduled_at,
        )
        db.add(task)

        db.add(Activity(
            company_id=contact.company_id, contact_id=contact_id, user_id=user.id,
            activity_type="step_added",
            content=f"Added {req.step_type} step to sequence: {req.subject}",
        ))

    await db.commit()
    await db.refresh(step)

    return {
        "id": step.id,
        "step_type": step.step_type,
        "subject": step.subject,
        "sequence_order": step.sequence_order,
        "send_delay_days": step.send_delay_days,
    }


# ============================================================
# Mark a non-email step as completed
# ============================================================

@router.post("/email/{email_id}/complete")
async def complete_step(
    email_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Mark a LinkedIn/call/text/custom step as done (BDR completed the task)."""
    step = (await db.execute(select(GeneratedEmail).where(GeneratedEmail.id == email_id))).scalar_one_or_none()
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")

    step.is_sent = True
    step.sent_at = datetime.now(timezone.utc)

    db.add(Activity(
        company_id=step.company_id, contact_id=step.contact_id, user_id=user.id,
        activity_type=f"{step.step_type}_completed",
        content=f"Completed: {step.subject}",
    ))

    await db.commit()
    return {"id": step.id, "completed": True}


# ============================================================
# Pause / resume sequence (manual, in addition to auto-pause-on-reply)
# ============================================================

@router.post("/contact/{contact_id}/pause")
async def pause_sequence(
    contact_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    now = datetime.now(timezone.utc)
    pending = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.contact_id == contact_id,
            GeneratedEmail.is_sent == False,
            GeneratedEmail.paused_at.is_(None),
        )
    )).scalars().all()
    for e in pending:
        e.paused_at = now
    db.add(Activity(company_id=contact.company_id, contact_id=contact.id, user_id=user.id,
                    activity_type="sequence_paused",
                    content=f"Sequence paused for {contact.full_name or contact.email or 'contact'} ({len(pending)} emails)"))
    await db.commit()
    return {"paused": len(pending)}


@router.post("/contact/{contact_id}/resume")
async def resume_sequence(
    contact_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    paused = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.contact_id == contact_id,
            GeneratedEmail.paused_at.isnot(None),
            GeneratedEmail.is_sent == False,
        )
    )).scalars().all()
    for e in paused:
        e.paused_at = None
    db.add(Activity(company_id=contact.company_id, contact_id=contact.id, user_id=user.id,
                    activity_type="sequence_resumed",
                    content=f"Sequence resumed ({len(paused)} emails)"))
    await db.commit()
    return {"resumed": len(paused)}


# ============================================================
# Profile (user settings — unchanged surface)
# ============================================================

class UpdateProfileRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    nickname: Optional[str] = None
    phone_number: Optional[str] = None
    scheduling_url: Optional[str] = None
    sending_enabled: Optional[bool] = None
    # Phase 4: dial preferences
    personal_phone_number: Optional[str] = None  # E.164, used for bridge mode
    dial_mode: Optional[str] = None  # 'browser' | 'bridge'
    timezone: Optional[str] = None


async def _profile_payload(db: AsyncSession, user: User) -> dict:
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
        "role": user.role,
        "twilio_phone_number": user.twilio_phone_number,
        "personal_phone_number": user.personal_phone_number or "",
        "dial_mode": user.dial_mode or "browser",
        "send_from": sender["from_email"],
        "reply_to": sender["reply_to"],
        "signature_html": await render_signature(db, user),
        "brief_enabled": bool(getattr(user, "brief_enabled", True)),
        "brief_hour": int(getattr(user, "brief_hour", 7) or 7),
        "timezone": getattr(user, "timezone", None) or "America/Phoenix",
        "last_brief_sent_at": user.last_brief_sent_at.isoformat() if getattr(user, "last_brief_sent_at", None) else None,
        "voicemail_greeting_url": getattr(user, "voicemail_greeting_url", None),
    }


@router.get("/profile")
async def get_profile(db: AsyncSession = Depends(get_tenant_db), user: User = Depends(get_current_user)):
    return await _profile_payload(db, user)


@router.patch("/profile")
async def update_profile(
    req: UpdateProfileRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    for field in ("first_name", "last_name", "nickname", "phone_number",
                  "scheduling_url", "personal_phone_number"):
        val = getattr(req, field)
        if val is not None:
            setattr(user, field, val.strip() or None if field == "personal_phone_number" else val.strip())
    if req.sending_enabled is not None:
        user.sending_enabled = req.sending_enabled
    if req.dial_mode is not None and req.dial_mode in ("browser", "bridge"):
        user.dial_mode = req.dial_mode
    if req.timezone is not None:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(req.timezone)
            user.timezone = req.timezone
        except (KeyError, Exception):
            pass
    await db.commit()
    await db.refresh(user)
    return await _profile_payload(db, user)


# ============================================================
# Resend webhook — auto-pause on reply, auto-qualify on click
# ============================================================

async def _advance_deal_from_sequence(db: AsyncSession, company_id: int) -> None:
    """When a prospect engages (open 3+ / click / reply), promote their
    deal out of in_sequence into the first configured middle stage
    (defaults to "qualified"). Restores the dollar value from the
    package since in_sequence deals carry value=0."""
    from app.routes.deal_routes import package_monthly_value
    from app.services import pipeline_config as _pc
    target_stage = await _pc.get_default_middle_stage_key(db)
    target_prob = await _pc.get_stage_probability(db, target_stage)
    deals = (await db.execute(
        select(Deal).where(Deal.company_id == company_id, Deal.stage == "in_sequence")
    )).scalars().all()
    for deal in deals:
        deal.stage = target_stage
        deal.probability = target_prob
        if deal.package and deal.value == 0:
            deal.value = package_monthly_value(deal.package)


async def _create_engagement_task(
    db: AsyncSession,
    company: Company,
    contact_id: int | None,
    reason: str,
) -> None:
    """Create a follow-up task for the deal owner (or company owner) when a contact engages.
    Skips if there's already an open engagement task for this company in the last 3 days
    so we don't spam the owner with duplicates on every open."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    existing = (await db.execute(
        select(Task).where(
            Task.company_id == company.id,
            Task.completed == False,
            Task.created_at >= cutoff,
            Task.description.like("Follow up%"),
        )
    )).first()
    if existing:
        return

    # Find an owner: deal assigned_to → company assigned_to → first user
    owner_id = None
    open_deal = (await db.execute(
        select(Deal).where(
            Deal.company_id == company.id,
            Deal.stage.in_(("prospecting", "qualified", "proposal", "negotiation")),
        ).order_by(Deal.created_at.desc())
    )).scalar_one_or_none()
    if open_deal and open_deal.assigned_to:
        owner_id = open_deal.assigned_to
    if not owner_id:
        owner_id = company.assigned_to
    if not owner_id:
        first_user = (await db.execute(select(User).order_by(User.id).limit(1))).scalar_one_or_none()
        owner_id = first_user.id if first_user else None
    if not owner_id:
        return

    contact_label = "the prospect"
    if contact_id:
        c = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
        if c and c.full_name:
            contact_label = c.full_name
        elif c and c.email:
            contact_label = c.email

    db.add(Task(
        company_id=company.id,
        contact_id=contact_id,
        deal_id=open_deal.id if open_deal else None,
        user_id=owner_id,
        description=f"Follow up with {contact_label} at {company.name} — {reason}",
        due_date=datetime.now(timezone.utc) + timedelta(days=1),
    ))
    db.add(Activity(
        company_id=company.id,
        contact_id=contact_id,
        deal_id=open_deal.id if open_deal else None,
        user_id=None,
        activity_type="task_auto_created",
        content=f"Auto-task: follow up — {reason}",
    ))


async def _resolve_resend_webhook_secret() -> str:
    """DB-first with env fallback — same pattern as the inbound route so
    the secret can be rotated from the Settings UI without an env change."""
    try:
        from app.runtime_config import get_resend_webhook_secret
        from app.database import async_session
        async with async_session() as _db:
            return await get_resend_webhook_secret(_db)
    except Exception:
        return (settings.resend_webhook_secret or "").strip()


def _verify_svix(raw_body: bytes, headers: dict, secret: str) -> bool:
    """Verify a Svix-signed webhook (Resend uses Svix). When no secret is
    configured we accept the request — necessary for bootstrap before the
    operator pastes the secret into Settings."""
    if not secret:
        return True
    try:
        from svix.webhooks import Webhook  # type: ignore
    except ImportError:
        import logging
        logging.getLogger("bmp.resend_webhook").error(
            "svix library not installed — accepting unverified payload"
        )
        return True
    try:
        Webhook(secret).verify(raw_body, headers)
        return True
    except Exception as e:
        import logging
        logging.getLogger("bmp.resend_webhook").warning(
            f"signature verification failed: {e}"
        )
        return False


@router.post("/webhook/resend")
async def resend_webhook(request: Request, db: AsyncSession = Depends(get_tenant_db)):
    """
    Handle Resend webhook events.
    Events: email.sent, email.delivered, email.opened, email.clicked,
            email.bounced, email.complained, email.delivery_delayed
    Plus: synthetic 'email.replied' (we don't get this from Resend; it comes from
    a Gmail forwarding webhook handler — Tier 1 follow-up work).

    Auth: Svix signatures (svix-id / svix-timestamp / svix-signature headers).
    Without verification, anyone who guesses the URL could mark prospects as
    bounced / complained — which would silently pause their sequences and
    bias the deliverability dashboard.
    """
    import logging
    log = logging.getLogger("bmp.resend_webhook")
    payload_bytes = await request.body()

    secret = await _resolve_resend_webhook_secret()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not _verify_svix(payload_bytes, headers, secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = await request.json()
    except Exception:
        return {"status": "invalid payload"}

    event_type = payload.get("type", "")
    data = payload.get("data", {})
    from_addr = data.get("from", "") or ""

    log.info(f"WEBHOOK type={event_type} from={from_addr[:60]} tags={data.get('tags', [])}")

    tags = {t["name"]: t["value"] for t in data.get("tags", []) if "name" in t}
    # Resend also puts our custom IDs in headers (X-Company-ID, X-Contact-ID, X-Email-ID)
    headers = {h["name"]: h["value"] for h in data.get("headers", []) if "name" in h and "value" in h}
    company_id = tags.get("company_id") or headers.get("X-Company-ID")
    contact_id = tags.get("contact_id") or headers.get("X-Contact-ID")
    email_id = tags.get("email_id") or headers.get("X-Email-ID")

    # Backwards compat: old emails have lead_id tag (now equals company_id since IDs were preserved)
    if not company_id:
        company_id = tags.get("lead_id")

    if not company_id:
        return {"status": "ok", "note": "no company_id tag"}
    company_id = int(company_id)

    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        return {"status": "ok", "note": "company not found"}

    now = datetime.now(timezone.utc)
    log.info(f"event={event_type} company={company_id} contact={contact_id} email={email_id}")

    em: Optional[GeneratedEmail] = None
    if email_id:
        em = (await db.execute(select(GeneratedEmail).where(GeneratedEmail.id == int(email_id)))).scalar_one_or_none()

    if event_type == "email.delivered":
        if em:
            if not em.is_sent:
                em.is_sent = True
                em.sent_at = em.sent_at or now
            if not em.delivered_at:
                em.delivered_at = now
        await db.commit()

    elif event_type == "email.opened":
        # Per-email open count + first-open timestamp on the GeneratedEmail row.
        # The open_count keeps every event for analytics, but timeline +
        # auto-qualify use the deduped first-open signal so image-proxy
        # prefetches (Apple Mail Privacy, Gmail Image Proxy, Outlook
        # preview) don't inflate engagement scores.
        is_first_open_for_email = False
        if em:
            is_first_open_for_email = em.opened_at is None
            if is_first_open_for_email:
                em.opened_at = now
            em.open_count = (em.open_count or 0) + 1

        # Auto-qualify when the recipient has opened 3+ DISTINCT emails.
        # Re-opens of the same email don't accumulate. Flush so this open
        # is visible to the count below.
        await db.flush()
        distinct_opens = (await db.execute(
            select(func.count()).select_from(GeneratedEmail).where(
                GeneratedEmail.company_id == company_id,
                GeneratedEmail.opened_at.is_not(None),
            )
        )).scalar() or 0

        already_auto_qualified = "[Auto-qualified: opened" in (company.enrichment_summary or "")
        if (distinct_opens >= 3 and company.status in ("sequencing", "contacted")
                and not already_auto_qualified):
            company.status = "qualified"
            company.enrichment_summary = (company.enrichment_summary or "") + (
                f" [Auto-qualified: opened {distinct_opens} distinct emails]"
            )
            await _advance_deal_from_sequence(db, company.id)
            await _create_engagement_task(db, company, int(contact_id) if contact_id else None,
                                          reason=f"opened {distinct_opens} distinct emails")
        elif is_first_open_for_email:
            # Lightweight breadcrumb — only on first open per email so
            # re-opens don't pile up tokens in the summary string.
            company.enrichment_summary = (company.enrichment_summary or "") + " [opened]"

        # Timeline entry: one email_opened Activity per (email_id, contact).
        # Re-opens by image proxies bump em.open_count but don't spawn
        # additional Activity rows that would poison the lead score.
        if contact_id and em and is_first_open_for_email:
            db.add(Activity(
                company_id=company_id, contact_id=int(contact_id),
                activity_type="email_opened", content="Email opened",
                metadata_json=json.dumps({"email_id": em.id}),
            ))
        await db.commit()

    elif event_type == "email.clicked":
        # We turned off Resend's click tracking — our /t/{token} wrapper owns
        # clicks now. If a stray event lands (e.g. an in-flight email sent
        # before we flipped the dashboard toggle), still attribute it
        # correctly so we don't lose data.
        log.info(f"email.clicked received — note that our own /t/{{token}} is the canonical source")
        if company.status in ("sequencing", "contacted"):
            company.status = "qualified"
            company.enrichment_summary = (company.enrichment_summary or "") + " [Auto-qualified: clicked link]"
            await _advance_deal_from_sequence(db, company.id)
        if contact_id:
            db.add(Activity(company_id=company_id, contact_id=int(contact_id),
                            activity_type="email_clicked", content="Email link clicked"))
        await _create_engagement_task(db, company, int(contact_id) if contact_id else None,
                                      reason="clicked a link in your email")
        await db.commit()

    elif event_type == "email.bounced":
        if em and not em.bounced_at:
            em.bounced_at = now
        company.status = "not_interested"
        company.enrichment_summary = (company.enrichment_summary or "") + " [Email bounced]"
        if contact_id:
            c = (await db.execute(select(Contact).where(Contact.id == int(contact_id)))).scalar_one_or_none()
            if c:
                c.email_status = "bounced"
                # Pause this contact's remaining sequence
                pending = (await db.execute(
                    select(GeneratedEmail).where(
                        GeneratedEmail.contact_id == c.id,
                        GeneratedEmail.is_sent == False,
                        GeneratedEmail.paused_at.is_(None),
                    )
                )).scalars().all()
                for e in pending:
                    e.paused_at = now
            db.add(Activity(company_id=company_id, contact_id=int(contact_id),
                            activity_type="email_bounced", content="Email bounced; sequence paused"))
        await db.commit()

    elif event_type == "email.complained":
        if em and not em.complained_at:
            em.complained_at = now
        company.status = "not_interested"
        company.enrichment_summary = (company.enrichment_summary or "") + " [Marked as spam]"
        if contact_id:
            db.add(Activity(company_id=company_id, contact_id=int(contact_id),
                            activity_type="email_complained", content="Marked as spam"))
        await db.commit()

    elif event_type == "email.replied":
        # Synthetic event — auto-pause this contact's sequence
        if contact_id:
            c = (await db.execute(select(Contact).where(Contact.id == int(contact_id)))).scalar_one_or_none()
            if c:
                pending = (await db.execute(
                    select(GeneratedEmail).where(
                        GeneratedEmail.contact_id == c.id,
                        GeneratedEmail.is_sent == False,
                        GeneratedEmail.paused_at.is_(None),
                    )
                )).scalars().all()
                now = datetime.now(timezone.utc)
                for e in pending:
                    e.paused_at = now
                if company.status in ("sequencing", "contacted"):
                    company.status = "replied"
                db.add(Activity(company_id=company_id, contact_id=int(contact_id),
                                activity_type="email_replied",
                                content=f"Reply received; sequence auto-paused ({len(pending)} emails)"))
        await db.commit()

    # ============================================================
    # Auto-sync Missive label after any of the above status changes
    # (best-effort, fire-and-forget — never blocks the webhook reply)
    # ============================================================
    if event_type in ("email.bounced", "email.complained", "email.replied", "email.opened", "email.clicked"):
        try:
            from app.services.missive_client import is_configured as _missive_ok, sync_status_label
            if _missive_ok() and contact_id:
                c2 = (await db.execute(select(Contact).where(Contact.id == int(contact_id)))).scalar_one_or_none()
                if c2 and c2.missive_conversation_id and company and company.status:
                    contact_name = f"{(c2.first_name or '').strip()} {(c2.last_name or '').strip()}".strip() or (c2.email or "")
                    import asyncio as _asyncio
                    _asyncio.create_task(sync_status_label(
                        conversation_id=c2.missive_conversation_id,
                        new_status=company.status,
                        contact_name=contact_name,
                        company_name=company.name or "",
                        actor="Prospector (auto)",
                    ))
        except Exception:
            # Sidecar action — must never break the webhook
            log.exception("Missive auto-sync skipped")

    return {"status": "ok", "event": event_type, "company_id": company_id}


# ============================================================
# Ad-hoc one-off email — outside any sequence
# ============================================================
#
# Use case: a BDR talks to a prospect on the phone and wants to fire a
# personalized follow-up that doesn't fit the templated sequence. Composer
# accepts rich-text HTML from the contenteditable editor on the frontend;
# we sanitize, wrap URLs through /t/{token} for click tracking, send via
# the same Resend path, and persist a GeneratedEmail row marked as 'adhoc'
# so the email lives in the contact's history (replies attribute back, the
# auto-pause-on-reply listener still works).
# ============================================================

class SendAdHocEmailRequest(BaseModel):
    contact_id: int
    subject: str
    html_body: str  # raw innerHTML from the rich-text editor
    sequence_label: Optional[str] = None  # if BDR also wants to associate it with a labeled sequence (rare)


@router.post("/adhoc")
async def send_adhoc_email(
    req: SendAdHocEmailRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Send a one-off custom email to a contact. Sanitizes the HTML body,
    wraps URLs for click tracking, persists as a GeneratedEmail row with
    step_type='adhoc' (so replies/clicks/opens flow through the existing
    listeners), and logs an email_sent Activity."""
    contact = (await db.execute(select(Contact).where(Contact.id == req.contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if not contact.email:
        raise HTTPException(status_code=400, detail="Contact has no email address.")
    if contact.unsubscribed_at:
        raise HTTPException(status_code=400, detail="Contact has unsubscribed.")
    if contact.email_status == "invalid":
        raise HTTPException(status_code=400, detail="Contact email is marked invalid. Verify or update before sending.")

    company = (await db.execute(select(Company).where(Company.id == contact.company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    if not settings.resend_api_key:
        raise HTTPException(status_code=500, detail="Email service not configured")
    if not user.sending_enabled:
        raise HTTPException(status_code=403, detail="Sending is disabled for your account.")

    subject = (req.subject or "").strip()
    if not subject:
        raise HTTPException(status_code=400, detail="Subject is required")

    from app.services.html_sanitize import sanitize_email_html
    clean_body = sanitize_email_html(req.html_body or "")
    if not clean_body:
        raise HTTPException(status_code=400, detail="Body is empty after sanitization")

    # Persist a GeneratedEmail row first so we have an email_id for tracking +
    # the Resend webhook can attribute opens/clicks/replies back to a row.
    ge = GeneratedEmail(
        contact_id=contact.id,
        company_id=company.id,
        step_type="adhoc",
        email_type="adhoc",
        subject=subject,
        body=clean_body,  # store the sanitized HTML so resend / regen later sees what went out
        sequence_order=0,
        send_delay_days=0,
        scheduled_send_at=datetime.now(timezone.utc),
        is_sent=False,  # flipped to True after Resend confirms
        auto_execute=False,
        sequence_label=(req.sequence_label or "adhoc"),
    )
    db.add(ge)
    await db.flush()

    sender = get_sender_info(user.first_name, user.full_name)
    from app.services.email_sender import generate_reply_token, reply_to_for_token
    ge.reply_token = generate_reply_token()
    sender["reply_to"] = reply_to_for_token(ge.reply_token)
    from app.services.tracking import wrap_html_links
    tracked_body = await wrap_html_links(
        db, clean_body, contact_id=contact.id, company_id=company.id, email_id=ge.id, label="body_link",
    )
    sig_html = await render_signature(db, user)
    tracked_signature = await wrap_html_links(
        db, sig_html, contact_id=contact.id, company_id=company.id, email_id=ge.id, label="signature_link",
    )

    result = await send_email(
        to_email=contact.email,
        subject=subject,
        body=tracked_body,
        from_name=sender["from_name"],
        from_firstname=sender["from_firstname"],
        reply_to_email=sender["reply_to"],
        company_id=company.id,
        contact_id=contact.id,
        email_id=ge.id,
        signature_html=tracked_signature,
        unsubscribe_token=contact.unsubscribe_token,
    )

    if not result.get("success"):
        # Roll back the GeneratedEmail row so a failed send doesn't leave a
        # ghost in the contact's history.
        await db.delete(ge)
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Failed to send: {result.get('error', 'unknown')}")

    ge.is_sent = True
    ge.sent_at = datetime.now(timezone.utc)
    ge.sent_by_user_id = user.id
    db.add(Activity(
        company_id=company.id, contact_id=contact.id, user_id=user.id,
        activity_type="email_sent",
        content=f"[Ad-hoc] Sent: {subject}",
    ))
    from app.services.credit_meter import meter, make_idem_key
    await meter(
        db, action_type="email_send",
        idempotency_key=make_idem_key("email_send", ge.id),
        user_id=user.id, action_ref=f"generated_email:{ge.id}",
    )
    await db.commit()
    return {
        "success": True,
        "email_id": ge.id,
        "resend_id": result.get("resend_id"),
        "sent_to": contact.email,
        "from": sender["from_email"],
        "reply_to": sender["reply_to"],
        "subject": subject,
    }
