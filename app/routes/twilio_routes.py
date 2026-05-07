"""
Twilio admin routes — Phase 1: number management only.

All endpoints require role='admin'. Phase 2 (click-to-call) and beyond
add the dialer + webhook routes.
"""
from __future__ import annotations
from typing import Optional, List
from datetime import datetime, timezone
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db, async_session
from app.models import User, Contact, Company, Activity
from app.auth import get_current_user
from app.runtime_config import get_twilio_credentials
from app.services.twilio_voice import (
    search_available_numbers,
    buy_number,
    list_owned_numbers,
    release_number,
    number_to_dict,
    generate_access_token,
    build_outbound_twiml,
    TwilioError,
)
from app.config import settings

router = APIRouter(prefix="/api/twilio", tags=["twilio"])


def _admin_only(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


async def _creds_or_400(db: AsyncSession):
    creds = await get_twilio_credentials(db)
    if not creds.is_minimally_configured:
        raise HTTPException(status_code=400,
                            detail="Twilio not configured. Set Account SID + Auth Token in Settings → API Keys.")
    return creds


# ============================================================
# Number management
# ============================================================

@router.get("/numbers/available")
async def numbers_available(
    area_code: Optional[str] = None,
    contains: Optional[str] = None,
    iso_country: str = "US",
    limit: int = 10,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Search Twilio's inventory for buyable numbers (typically by area code)."""
    _admin_only(user)
    creds = await _creds_or_400(db)
    try:
        numbers = await search_available_numbers(
            creds, area_code=area_code, contains=contains,
            iso_country=iso_country, limit=limit,
        )
    except TwilioError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return [number_to_dict(n) for n in numbers]


class BuyNumberRequest(BaseModel):
    phone_number: str  # E.164, e.g. "+17025551234"


@router.post("/numbers/buy")
async def numbers_buy(
    req: BuyNumberRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Provision a number into the Twilio account. Costs $1.15/mo per number."""
    _admin_only(user)
    creds = await _creds_or_400(db)
    try:
        n = await buy_number(creds, req.phone_number)
    except TwilioError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return number_to_dict(n)


@router.get("/numbers/owned")
async def numbers_owned(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List numbers we own + which user (if any) is assigned to each."""
    _admin_only(user)
    creds = await _creds_or_400(db)
    try:
        numbers = await list_owned_numbers(creds)
    except TwilioError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Cross-reference with user assignments
    users_result = await db.execute(select(User).where(User.twilio_phone_number.isnot(None)))
    by_phone = {u.twilio_phone_number: u for u in users_result.scalars().all()}

    out = []
    for n in numbers:
        d = number_to_dict(n)
        owner = by_phone.get(n.phone_number)
        d["assigned_to"] = (
            {"id": owner.id, "name": owner.full_name, "email": owner.email}
            if owner else None
        )
        out.append(d)
    return out


@router.delete("/numbers/{phone_sid}")
async def numbers_release(
    phone_sid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Release a number back to Twilio (stops the $1.15/mo billing).
    Also clears the assignment from any user that had it."""
    _admin_only(user)
    creds = await _creds_or_400(db)
    try:
        # Find any user that had this number assigned and clear the field.
        # We need the phone_number (not just SID) — fetch from owned list.
        owned = await list_owned_numbers(creds)
        match = next((n for n in owned if n.sid == phone_sid), None)

        await release_number(creds, phone_sid)

        if match and match.phone_number:
            users_result = await db.execute(
                select(User).where(User.twilio_phone_number == match.phone_number)
            )
            for u in users_result.scalars().all():
                u.twilio_phone_number = None
            await db.commit()
    except TwilioError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"released": True}


# ============================================================
# Assign / unassign a number to a team member
# ============================================================

class AssignTwilioRequest(BaseModel):
    phone_number: Optional[str] = None  # E.164, or null to clear


@router.patch("/users/{user_id}/twilio")
async def assign_twilio_number(
    user_id: int,
    req: AssignTwilioRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Assign or unassign a Twilio number to a team member (admin only)."""
    _admin_only(user)
    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Ensure no two users share the same number
    if req.phone_number:
        clash = (await db.execute(
            select(User).where(User.twilio_phone_number == req.phone_number, User.id != user_id)
        )).scalar_one_or_none()
        if clash:
            raise HTTPException(status_code=400,
                                detail=f"Number {req.phone_number} is already assigned to {clash.full_name or clash.email}")

    target.twilio_phone_number = (req.phone_number or "").strip() or None
    if not target.twilio_identity:
        target.twilio_identity = f"bmp_user_{target.id}"
    await db.commit()
    await db.refresh(target)
    return {
        "user_id": target.id,
        "twilio_phone_number": target.twilio_phone_number,
        "twilio_identity": target.twilio_identity,
    }


# ============================================================
# Voice SDK access token (BDR's browser fetches this every ~50 min)
# ============================================================

@router.post("/voice/token")
async def voice_token(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Mint a Twilio Voice SDK access token for the current rep."""
    if not user.twilio_phone_number:
        raise HTTPException(status_code=400,
                            detail="You don't have a Twilio number assigned. Ask an admin to assign one in Settings.")
    if not user.twilio_identity:
        # Backfill if somehow missing
        user.twilio_identity = f"bmp_user_{user.id}"
        await db.commit()

    creds = await get_twilio_credentials(db)
    if not creds.is_voice_sdk_ready:
        raise HTTPException(status_code=400,
                            detail="Twilio not fully configured for browser dialing. "
                                   "Need API Key SID, API Key Secret, and TwiML App SID in Settings.")
    try:
        jwt_token = generate_access_token(creds, identity=user.twilio_identity, ttl_seconds=3600)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "token": jwt_token,
        "identity": user.twilio_identity,
        "from_number": user.twilio_phone_number,
        "expires_in": 3600,
    }


# ============================================================
# Twilio webhooks — public endpoints called by Twilio infrastructure
# ============================================================

@router.post("/voice/twiml")
async def voice_twiml(request: Request):
    """
    TwiML endpoint Twilio hits when the SDK initiates an outbound call.
    Receives From=client:bmp_user_X, To={number to dial}.
    Returns TwiML: <Dial callerId={rep's number} record>{To}</Dial>
    """
    form = await request.form()
    from_identity_raw = form.get("From", "")  # e.g. "client:bmp_user_3"
    to_number = form.get("To", "").strip()

    if not to_number:
        return Response(
            content="<Response><Say>Missing destination number.</Say></Response>",
            media_type="application/xml",
        )

    # Resolve the rep's caller ID by their identity
    identity = from_identity_raw.replace("client:", "") if from_identity_raw else ""
    caller_id = None
    if identity:
        async with async_session() as db:
            u = (await db.execute(select(User).where(User.twilio_identity == identity))).scalar_one_or_none()
            if u and u.twilio_phone_number:
                caller_id = u.twilio_phone_number

    if not caller_id:
        return Response(
            content="<Response><Say>This rep does not have a verified caller ID assigned.</Say></Response>",
            media_type="application/xml",
        )

    recording_callback = f"{settings.public_url.rstrip('/')}/api/twilio/voice/recording"
    twiml = build_outbound_twiml(
        to_number=to_number,
        caller_id=caller_id,
        record_calls=True,
        recording_status_callback=recording_callback,
        consent_disclosure=True,
    )
    return Response(content=twiml, media_type="application/xml")


@router.post("/voice/status")
async def voice_status(request: Request):
    """
    Status callback from Twilio. Fires on call lifecycle events.
    We persist duration once 'completed' fires.
    """
    form = await request.form()
    call_sid = form.get("CallSid")
    call_status = form.get("CallStatus", "")
    duration = form.get("CallDuration")

    if not call_sid:
        return Response(content="", media_type="application/xml")

    async with async_session() as db:
        act = (await db.execute(
            select(Activity).where(Activity.twilio_call_sid == call_sid)
        )).scalar_one_or_none()
        if act:
            if duration:
                try:
                    act.call_duration_seconds = int(duration)
                except (TypeError, ValueError):
                    pass
            # Map status to outcome if not already set
            if not act.call_outcome:
                if call_status == "completed":
                    act.call_outcome = "connected" if (duration and int(duration) > 0) else "no_answer"
                elif call_status == "busy":
                    act.call_outcome = "busy"
                elif call_status in ("failed", "canceled"):
                    act.call_outcome = "failed"
                elif call_status == "no-answer":
                    act.call_outcome = "no_answer"
            await db.commit()

    return Response(content="", media_type="application/xml")


@router.post("/voice/recording")
async def voice_recording(request: Request):
    """Recording-complete webhook. Saves URL onto the matching Activity."""
    form = await request.form()
    call_sid = form.get("CallSid")
    recording_url = form.get("RecordingUrl")
    if not (call_sid and recording_url):
        return Response(content="", media_type="application/xml")

    async with async_session() as db:
        act = (await db.execute(
            select(Activity).where(Activity.twilio_call_sid == call_sid)
        )).scalar_one_or_none()
        if act:
            act.recording_url = recording_url
            await db.commit()
    # Phase 3 will kick off transcription here.
    return Response(content="", media_type="application/xml")


# ============================================================
# Frontend posts here when the dialer modal closes
# ============================================================

class LogCallRequest(BaseModel):
    call_sid: str
    contact_id: Optional[int] = None  # null when calling a company main line w/o primary contact
    company_id: Optional[int] = None  # required when contact_id is null
    duration_seconds: int = 0
    direction: str = "outbound"  # outbound | inbound
    outcome: Optional[str] = None  # connected, voicemail, no_answer, ...
    notes: str = ""


@router.post("/voice/log-call")
async def log_call(
    req: LogCallRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create an Activity row when the dialer modal closes.
    Requires at least one of contact_id / company_id."""
    contact = None
    company_id = req.company_id
    if req.contact_id:
        contact = (await db.execute(select(Contact).where(Contact.id == req.contact_id))).scalar_one_or_none()
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")
        company_id = contact.company_id
    elif not company_id:
        raise HTTPException(status_code=400, detail="Either contact_id or company_id is required")

    # Don't double-create — the Twilio status webhook may have made a stub
    existing = (await db.execute(
        select(Activity).where(Activity.twilio_call_sid == req.call_sid)
    )).scalar_one_or_none()

    summary = req.notes or {
        "connected": "Call connected — see notes",
        "voicemail": "Left a voicemail",
        "no_answer": "No answer",
        "busy": "Line busy",
        "wrong_number": "Wrong number",
        "gatekeeper": "Reached gatekeeper",
        "declined": "Declined / not interested",
    }.get(req.outcome or "", "Call logged")

    if existing:
        if contact:
            existing.contact_id = contact.id
        existing.user_id = user.id
        existing.content = summary
        existing.call_duration_seconds = req.duration_seconds or existing.call_duration_seconds
        existing.call_direction = req.direction
        if req.outcome:
            existing.call_outcome = req.outcome
        await db.commit()
        await db.refresh(existing)
        return _activity_to_dict(existing)

    activity = Activity(
        company_id=company_id,
        contact_id=(contact.id if contact else None),
        user_id=user.id,
        activity_type="call",
        content=summary,
        twilio_call_sid=req.call_sid,
        call_duration_seconds=req.duration_seconds,
        call_direction=req.direction,
        call_outcome=req.outcome,
        metadata_json=json.dumps({"logged_via": "dialer-modal"}),
    )
    db.add(activity)
    await db.commit()
    await db.refresh(activity)
    return _activity_to_dict(activity)


def _activity_to_dict(a: Activity) -> dict:
    return {
        "id": a.id,
        "company_id": a.company_id,
        "contact_id": a.contact_id,
        "type": a.activity_type,
        "content": a.content,
        "twilio_call_sid": a.twilio_call_sid,
        "call_duration_seconds": a.call_duration_seconds,
        "call_direction": a.call_direction,
        "call_outcome": a.call_outcome,
        "recording_url": a.recording_url,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }
