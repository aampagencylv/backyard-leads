"""
Twilio admin routes — Phase 1: number management only.

All endpoints require role='admin'. Phase 2 (click-to-call) and beyond
add the dialer + webhook routes.
"""
from __future__ import annotations
import asyncio
from typing import Optional, List
from datetime import datetime, timezone
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db, async_session
from app.models import User, Contact, Company, Activity, GeneratedEmail
from app.auth import get_current_user
from app.runtime_config import get_twilio_credentials
from urllib.parse import quote

from app.services.twilio_voice import (
    search_available_numbers,
    buy_number,
    list_owned_numbers,
    release_number,
    number_to_dict,
    normalize_phone_e164,
    generate_access_token,
    build_outbound_twiml,
    build_bridge_twiml,
    parse_inbound_twiml,
    build_voicemail_twiml,
    initiate_bridge_call,
    configure_inbound_voice_url,
    TwilioError,
)
from app.services.call_transcription import transcribe_and_summarize_in_background
from app.services.twilio_sms import (
    send_sms,
    configure_sms_webhook,
    check_send_window,
    is_stop_keyword,
    is_start_keyword,
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
    public = settings.public_url.rstrip('/')
    try:
        n = await buy_number(
            creds, req.phone_number,
            voice_url=f"{public}/api/twilio/voice/inbound",
            status_callback=f"{public}/api/twilio/voice/status",
        )
        # Set the SMS inbound webhook too so texts route somewhere
        if n and n.sid:
            try:
                await configure_sms_webhook(
                    creds, n.sid,
                    sms_url=f"{public}/api/twilio/sms/inbound",
                )
            except TwilioError:
                pass  # SMS webhook is best-effort; voice path already worked
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

    # Re-configure the assigned number's inbound webhook so calls actually
    # route to this rep's browser/personal phone.
    if target.twilio_phone_number:
        try:
            creds = await get_twilio_credentials(db)
            owned = await list_owned_numbers(creds)
            match = next((n for n in owned if n.phone_number == target.twilio_phone_number), None)
            if match and match.sid:
                public = settings.public_url.rstrip('/')
                await configure_inbound_voice_url(
                    creds, match.sid,
                    voice_url=f"{public}/api/twilio/voice/inbound",
                    status_callback=f"{public}/api/twilio/voice/status",
                )
                # SMS inbound webhook too — best effort
                try:
                    await configure_sms_webhook(
                        creds, match.sid,
                        sms_url=f"{public}/api/twilio/sms/inbound",
                    )
                except TwilioError:
                    pass
        except TwilioError:
            pass  # number is still assigned in our DB even if Twilio webhook setup fails

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
    to_raw = form.get("To", "").strip()

    to_number = normalize_phone_e164(to_raw)
    if not to_number:
        return Response(
            content=f"<Response><Say>Invalid destination number: {to_raw}</Say></Response>",
            media_type="application/xml",
        )

    # Resolve the rep's caller ID by their identity
    identity = from_identity_raw.replace("client:", "") if from_identity_raw else ""
    caller_id = None
    if identity:
        async with async_session() as db:
            u = (await db.execute(select(User).where(User.twilio_identity == identity))).scalar_one_or_none()
            if u and u.twilio_phone_number:
                caller_id = normalize_phone_e164(u.twilio_phone_number)

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
    """Recording-complete webhook. Saves URL onto the matching Activity and
    spawns an async task to transcribe + summarize via Deepgram + Claude.
    """
    form = await request.form()
    call_sid = form.get("CallSid")
    recording_url = form.get("RecordingUrl")
    if not (call_sid and recording_url):
        return Response(content="", media_type="application/xml")

    # Twilio gives us the .wav URL by default; .mp3 is smaller (~10x) and
    # all we need for transcription. Append .mp3 if the URL doesn't have an ext.
    if not recording_url.lower().endswith((".mp3", ".wav")):
        recording_url = recording_url + ".mp3"

    async with async_session() as db:
        act = (await db.execute(
            select(Activity).where(Activity.twilio_call_sid == call_sid)
        )).scalar_one_or_none()
        activity_id: Optional[int] = None
        if act:
            act.recording_url = recording_url
            await db.commit()
            activity_id = act.id

    # Fire-and-forget transcription pipeline. Don't block the webhook ack.
    if activity_id is not None:
        asyncio.create_task(transcribe_and_summarize_in_background(activity_id))

    return Response(content="", media_type="application/xml")


# ============================================================
# Phone bridge mode (CallRail-style outbound)
# ============================================================

class BridgeCallRequest(BaseModel):
    contact_id: Optional[int] = None
    company_id: Optional[int] = None
    to_number: str  # E.164 of the prospect


@router.post("/voice/bridge-call")
async def bridge_call(
    req: BridgeCallRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Start a two-leg outbound call:
      Twilio dials the rep's personal phone first, then bridges them to
      the prospect once they pick up. Browser stays out of the audio path.
    """
    if not user.personal_phone_number:
        raise HTTPException(status_code=400, detail="Add your personal phone number in Settings → Profile to use phone bridge mode.")
    if not user.twilio_phone_number:
        raise HTTPException(status_code=400, detail="No Twilio caller ID assigned to you. Ask an admin.")

    creds = await get_twilio_credentials(db)
    if not creds.is_minimally_configured:
        raise HTTPException(status_code=400, detail="Twilio not configured")

    # Normalize all phone numbers to E.164 BEFORE handing to Twilio.
    # The user might have entered a contact phone like "480-338-3369".
    to_e164 = normalize_phone_e164(req.to_number)
    if not to_e164:
        raise HTTPException(status_code=400, detail=f"Invalid prospect phone: {req.to_number}")
    rep_caller = normalize_phone_e164(user.twilio_phone_number)
    rep_personal = normalize_phone_e164(user.personal_phone_number)
    if not rep_caller or not rep_personal:
        raise HTTPException(status_code=400, detail="Your assigned Twilio number or personal phone is not in E.164 format. Update them in Settings.")

    # Build the bridge-twiml URL. URL-encode every value so the '+' in
    # E.164 numbers survives the round-trip (in query strings, raw '+'
    # decodes to a space).
    public = settings.public_url.rstrip('/')
    twiml_url = (
        f"{public}/api/twilio/voice/bridge-twiml"
        f"?to={quote(to_e164, safe='')}"
        f"&caller_id={quote(rep_caller, safe='')}"
    )
    status_url = f"{public}/api/twilio/voice/status"

    try:
        parent_sid = await initiate_bridge_call(
            creds,
            rep_personal_phone=rep_personal,
            rep_caller_id=rep_caller,
            bridge_twiml_url=twiml_url,
            status_callback_url=status_url,
        )
    except TwilioError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Pre-create a stub Activity so the bridge call shows up immediately
    company_id = req.company_id
    if req.contact_id and not company_id:
        c = (await db.execute(select(Contact).where(Contact.id == req.contact_id))).scalar_one_or_none()
        if c:
            company_id = c.company_id
    if company_id:
        db.add(Activity(
            company_id=company_id,
            contact_id=req.contact_id,
            user_id=user.id,
            activity_type="call",
            content="Bridge call initiated — ringing your phone",
            twilio_call_sid=parent_sid,
            call_direction="outbound",
        ))
        await db.commit()

    return {"call_sid": parent_sid, "ringing_your_phone": user.personal_phone_number}


@router.post("/voice/bridge-twiml")
async def bridge_twiml(request: Request):
    """TwiML returned when the rep's personal phone answers our bridge call."""
    qp = dict(request.query_params)
    # Re-normalize defensively — even if the upstream encoded properly, we
    # want the dial to work for any stray legacy URL.
    to_number = normalize_phone_e164(qp.get("to", ""))
    caller_id = normalize_phone_e164(qp.get("caller_id", ""))
    if not (to_number and caller_id):
        return Response(
            content=f"<Response><Say>Bridge call configuration is missing the prospect number or caller ID.</Say></Response>",
            media_type="application/xml",
        )
    recording_callback = f"{settings.public_url.rstrip('/')}/api/twilio/voice/recording"
    twiml = build_bridge_twiml(
        prospect_number=to_number,
        caller_id=caller_id,
        record_calls=True,
        recording_status_callback=recording_callback,
        consent_disclosure=True,
    )
    return Response(content=twiml, media_type="application/xml")


# ============================================================
# Inbound routing + voicemail
# ============================================================

@router.post("/voice/inbound")
async def voice_inbound(request: Request):
    """
    TwiML for INBOUND calls. Looks up which rep owns the called Twilio
    number, rings their browser AND personal phone simultaneously.
    Falls through to voicemail after 20s.
    """
    form = await request.form()
    called_number = form.get("Called", "")
    from_number = form.get("From", "")

    rep = None
    if called_number:
        async with async_session() as db:
            rep = (await db.execute(
                select(User).where(User.twilio_phone_number == called_number)
            )).scalar_one_or_none()

    if not rep:
        # No rep assigned to this number — go straight to voicemail
        public = settings.public_url.rstrip('/')
        recording_callback = f"{public}/api/twilio/voice/voicemail-recording"
        twiml = build_voicemail_twiml(
            company_name="Backyard Marketing Pros",
            recording_status_callback=recording_callback,
        )
        return Response(content=twiml, media_type="application/xml")

    public = settings.public_url.rstrip('/')
    voicemail_url = f"{public}/api/twilio/voice/inbound-voicemail?from={from_number}&rep_id={rep.id}"
    twiml = parse_inbound_twiml(
        rep_identity=rep.twilio_identity or f"bmp_user_{rep.id}",
        rep_personal_phone=rep.personal_phone_number,
        voicemail_action_url=voicemail_url,
        timeout=20,
    )
    return Response(content=twiml, media_type="application/xml")


@router.post("/voice/inbound-voicemail")
async def inbound_voicemail(request: Request):
    """When the inbound <Dial> doesn't connect (no answer), play voicemail TwiML."""
    qp = dict(request.query_params)
    rep_id = qp.get("rep_id")
    rep_first_name = None
    if rep_id:
        async with async_session() as db:
            rep = (await db.execute(select(User).where(User.id == int(rep_id)))).scalar_one_or_none()
            if rep:
                rep_first_name = rep.first_name

    public = settings.public_url.rstrip('/')
    recording_callback = f"{public}/api/twilio/voice/voicemail-recording?from={qp.get('from','')}&rep_id={rep_id or ''}"
    twiml = build_voicemail_twiml(
        company_name="Backyard Marketing Pros",
        rep_first_name=rep_first_name,
        recording_status_callback=recording_callback,
    )
    return Response(content=twiml, media_type="application/xml")


@router.post("/voice/voicemail-recording")
async def voicemail_recording(request: Request):
    """When a voicemail recording finishes, save it as an Activity."""
    form = await request.form()
    qp = dict(request.query_params)
    recording_url = form.get("RecordingUrl", "")
    call_sid = form.get("CallSid", "")
    duration = form.get("RecordingDuration", "0")
    from_number = qp.get("from", "")
    rep_id = qp.get("rep_id", "")

    if not recording_url:
        return Response(content="", media_type="application/xml")

    if not recording_url.lower().endswith((".mp3", ".wav")):
        recording_url = recording_url + ".mp3"

    async with async_session() as db:
        # Try to match the From number to a known Contact for attribution
        contact = None
        if from_number:
            contact = (await db.execute(
                select(Contact).where(Contact.phone == from_number)
            )).scalar_one_or_none()

        company_id = contact.company_id if contact else None
        if not company_id:
            return Response(content="", media_type="application/xml")  # orphan vm — Phase 4 won't auto-create company

        try:
            duration_int = int(duration)
        except ValueError:
            duration_int = 0

        rep_user_id = int(rep_id) if rep_id else None

        activity = Activity(
            company_id=company_id,
            contact_id=contact.id if contact else None,
            user_id=rep_user_id,
            activity_type="voicemail",
            content=f"Voicemail received from {contact.full_name or from_number}",
            twilio_call_sid=call_sid,
            call_direction="inbound",
            call_duration_seconds=duration_int,
            recording_url=recording_url,
            call_outcome="voicemail",
        )
        db.add(activity)
        await db.commit()
        await db.refresh(activity)
        # Kick off transcription pipeline for the voicemail
        asyncio.create_task(transcribe_and_summarize_in_background(activity.id))

    return Response(content="", media_type="application/xml")


@router.get("/inbound/lookup")
async def inbound_lookup(
    phone: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """When the browser receives an inbound call via the SDK, it calls this
    to resolve the From number to a Contact + Company so the modal pre-pops
    with CRM context."""
    contact = (await db.execute(
        select(Contact).where(Contact.phone == phone)
    )).scalar_one_or_none()
    if not contact:
        return {"contact": None, "company": None}
    company = (await db.execute(select(Company).where(Company.id == contact.company_id))).scalar_one_or_none()
    return {
        "contact": {
            "id": contact.id, "name": contact.full_name or contact.email,
            "title": contact.title, "email": contact.email, "phone": contact.phone,
            "company_id": contact.company_id,
        },
        "company": {
            "id": company.id, "name": company.name, "status": company.status,
        } if company else None,
    }


@router.get("/recording/{activity_id}")
async def proxy_recording(
    activity_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Proxy a Twilio recording through our auth — Twilio recording URLs
    require basic auth with the Account SID + Auth Token, which we can't
    safely embed in a browser <audio> tag. Browser hits this endpoint
    with our normal session auth; we re-fetch from Twilio with basic auth
    and stream the bytes back.
    """
    act = (await db.execute(select(Activity).where(Activity.id == activity_id))).scalar_one_or_none()
    if not act or not act.recording_url:
        raise HTTPException(status_code=404, detail="No recording on this activity")

    creds = await get_twilio_credentials(db)
    if not creds.is_minimally_configured:
        raise HTTPException(status_code=400, detail="Twilio not configured")

    # Stream from Twilio → user
    import httpx as _httpx
    async def stream():
        async with _httpx.AsyncClient(timeout=60) as client:
            async with client.stream("GET", act.recording_url, auth=(creds.account_sid, creds.auth_token)) as r:
                async for chunk in r.aiter_bytes(chunk_size=64 * 1024):
                    yield chunk

    from fastapi.responses import StreamingResponse
    media_type = "audio/mpeg" if act.recording_url.lower().endswith(".mp3") else "audio/wav"
    return StreamingResponse(stream(), media_type=media_type)


@router.post("/voice/transcribe-now/{activity_id}")
async def transcribe_now(
    activity_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Manual re-trigger for the transcription pipeline.
    Useful when the webhook was missed, the Deepgram key wasn't set yet, or
    we want to re-summarize after editing the prompt.
    """
    act = (await db.execute(select(Activity).where(Activity.id == activity_id))).scalar_one_or_none()
    if not act:
        raise HTTPException(status_code=404, detail="Activity not found")
    if not act.recording_url:
        raise HTTPException(status_code=400, detail="No recording URL on this activity yet")
    # Clear the previous transcript so the pipeline rebuilds it
    act.transcript = None
    act.call_summary = None
    await db.commit()
    asyncio.create_task(transcribe_and_summarize_in_background(activity_id))
    return {"queued": True, "activity_id": activity_id}


# ============================================================
# Frontend posts here when the dialer modal closes
# ============================================================

class LogCallRequest(BaseModel):
    call_sid: str
    contact_id: Optional[int] = None  # null when calling a company main line w/o primary contact
    company_id: Optional[int] = None  # required when contact_id is null
    to_number: Optional[str] = None   # the number we actually dialed (improves timeline summary)
    is_main_line: bool = False        # true when user clicked the company's main phone
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

    # Build a clear summary line — "Called {who} at {number} — outcome (mm:ss)"
    company = None
    if company_id:
        company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()

    if req.is_main_line and company:
        who = f"{company.name} main line"
    elif contact:
        who = contact.full_name or contact.email or "the prospect"
    elif company:
        who = company.name
    else:
        who = "unknown"

    dialed = normalize_phone_e164(req.to_number) or req.to_number or ""
    duration_str = ""
    if req.duration_seconds:
        m, s = divmod(req.duration_seconds, 60)
        duration_str = f" ({m}:{s:02d})"

    outcome_words = {
        "connected": "connected",
        "voicemail": "left voicemail",
        "no_answer": "no answer",
        "busy": "line busy",
        "wrong_number": "wrong number",
        "gatekeeper": "reached gatekeeper",
        "declined": "declined",
        "failed": "failed",
    }
    outcome_str = outcome_words.get(req.outcome or "", "")
    auto_summary_parts = [f"Called {who}"]
    if dialed:
        auto_summary_parts.append(f"at {dialed}")
    head = " ".join(auto_summary_parts)
    if outcome_str:
        head = f"{head} — {outcome_str}{duration_str}"
    elif duration_str:
        head = f"{head}{duration_str}"

    # If user wrote notes, attach them BELOW the summary line so the timeline
    # always identifies who/what was called even if the user typed something
    # tangential.
    if req.notes:
        summary = f"{head}\n{req.notes}"
    else:
        summary = head

    # Don't double-create — the Twilio status webhook may have made a stub
    existing = (await db.execute(
        select(Activity).where(Activity.twilio_call_sid == req.call_sid)
    )).scalar_one_or_none()

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

    # Sequence-engine listener: a real conversation pauses the sequence, just
    # like an email/iMessage reply does. Definition of "real": connected outcome
    # AND duration > 30s (filters out voicemails / no-answers / 5-second misdials).
    if contact and (req.outcome or "") == "connected" and (req.duration_seconds or 0) > 30:
        from app.services.sequence_engine import pause_sequence
        try:
            await pause_sequence(db, contact.id, reason="connected call >30s")
            await db.commit()
        except Exception:
            pass  # listener failure shouldn't block the call log

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


# ============================================================
# SMS — outbound + inbound + TCPA opt-out
# ============================================================

class SendSmsRequest(BaseModel):
    contact_id: int
    body: str
    bypass_send_window: bool = False  # admins-only override (e.g. urgent reply)


@router.post("/sms/send")
async def sms_send(
    req: SendSmsRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Send a one-off SMS to a contact. Logs to the timeline as
    type='sms_sent'. Refuses if contact has opted out (do_not_text=True)
    or if the send window check fails (and the user isn't an admin who
    explicitly bypassed it)."""
    contact = (await db.execute(select(Contact).where(Contact.id == req.contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if not contact.phone:
        raise HTTPException(status_code=400, detail="Contact has no phone number")
    if contact.do_not_text:
        raise HTTPException(status_code=400, detail="This contact has opted out of SMS (replied STOP).")
    if not user.twilio_phone_number:
        raise HTTPException(status_code=400, detail="No Twilio number assigned to you. Ask an admin.")

    # TCPA send-window check (8am-9pm contact-local) — admins can override
    window = check_send_window(contact.phone)
    if not window.allowed and not (req.bypass_send_window and user.role == "admin"):
        raise HTTPException(status_code=400, detail=window.reason + " Use the bypass option if this is a reply to a recent inbound message.")

    creds = await get_twilio_credentials(db)
    if not creds.is_minimally_configured:
        raise HTTPException(status_code=400, detail="Twilio not configured")

    public = settings.public_url.rstrip('/')
    status_callback = f"{public}/api/twilio/sms/status"

    result = await send_sms(
        creds,
        to_number=contact.phone,
        from_number=user.twilio_phone_number,
        body=req.body,
        status_callback=status_callback,
    )
    if not result.success:
        raise HTTPException(status_code=502, detail=f"Twilio rejected: {result.error}")

    activity = Activity(
        company_id=contact.company_id,
        contact_id=contact.id,
        user_id=user.id,
        activity_type="sms_sent",
        content=f"SMS to {contact.full_name or contact.phone}: {req.body}",
        metadata_json=json.dumps({
            "message_sid": result.message_sid,
            "from": user.twilio_phone_number,
            "to": contact.phone,
        }),
    )
    db.add(activity)
    await db.commit()
    await db.refresh(activity)
    return {
        "success": True,
        "message_sid": result.message_sid,
        "activity": _activity_to_dict(activity),
    }


@router.post("/sms/inbound")
async def sms_inbound(request: Request):
    """
    Twilio webhook for incoming SMS.
    1. Match the From number to a known Contact (any rep)
    2. Log as Activity type='sms_received'
    3. Auto-handle STOP keywords → set do_not_text + log opt-out
    4. Auto-handle START keywords → clear do_not_text
    5. Auto-pause active email sequence (parallel to email reply behavior)
    Returns TwiML; on STOP we send a short confirmation to the sender.
    """
    form = await request.form()
    from_number = (form.get("From") or "").strip()
    to_number = (form.get("To") or "").strip()
    body = (form.get("Body") or "").strip()
    message_sid = form.get("MessageSid") or form.get("SmsSid") or ""

    if not from_number:
        return Response(content="<Response/>", media_type="application/xml")

    async with async_session() as db:
        contact = (await db.execute(
            select(Contact).where(Contact.phone == from_number)
        )).scalar_one_or_none()

        # Find which rep owns the called number (so the activity attributes correctly)
        rep = None
        if to_number:
            rep = (await db.execute(
                select(User).where(User.twilio_phone_number == to_number)
            )).scalar_one_or_none()

        if not contact:
            # Unknown sender — log to nothing for now (could create a placeholder
            # Company + Contact in a future enhancement). Do still honor STOP.
            return Response(content="<Response/>", media_type="application/xml")

        # STOP: set do_not_text + log + reply with a confirmation
        if is_stop_keyword(body):
            contact.do_not_text = True
            contact.do_not_text_at = datetime.now(timezone.utc)
            db.add(Activity(
                company_id=contact.company_id, contact_id=contact.id,
                user_id=rep.id if rep else None,
                activity_type="sms_opt_out",
                content=f"SMS opt-out (STOP) from {contact.full_name or from_number}",
                metadata_json=json.dumps({"message_sid": message_sid, "body": body}),
            ))
            # Auto-pause any active email sequence too — strong signal they want to be left alone
            pending = (await db.execute(
                select(GeneratedEmail).where(
                    GeneratedEmail.contact_id == contact.id,
                    GeneratedEmail.is_sent == False,
                    GeneratedEmail.paused_at.is_(None),
                )
            )).scalars().all()
            now = datetime.now(timezone.utc)
            for e in pending:
                e.paused_at = now
            await db.commit()
            return Response(
                content="<Response><Message>You're unsubscribed. Reply START to resume.</Message></Response>",
                media_type="application/xml",
            )

        # START: clear opt-out
        if is_start_keyword(body) and contact.do_not_text:
            contact.do_not_text = False
            contact.do_not_text_at = None
            db.add(Activity(
                company_id=contact.company_id, contact_id=contact.id,
                user_id=rep.id if rep else None,
                activity_type="sms_opt_in",
                content=f"SMS opt-in restored (START) from {contact.full_name or from_number}",
            ))
            await db.commit()
            return Response(
                content="<Response><Message>You're back in. We'll only send what's actually useful.</Message></Response>",
                media_type="application/xml",
            )

        # Regular incoming message — log + auto-pause email sequence (replied)
        db.add(Activity(
            company_id=contact.company_id,
            contact_id=contact.id,
            user_id=rep.id if rep else None,
            activity_type="sms_received",
            content=f"SMS from {contact.full_name or from_number}: {body}",
            metadata_json=json.dumps({
                "message_sid": message_sid,
                "from": from_number,
                "to": to_number,
                "body": body,
            }),
        ))

        # Auto-pause email sequence on inbound SMS (matches email-reply behavior)
        pending = (await db.execute(
            select(GeneratedEmail).where(
                GeneratedEmail.contact_id == contact.id,
                GeneratedEmail.is_sent == False,
                GeneratedEmail.paused_at.is_(None),
            )
        )).scalars().all()
        now = datetime.now(timezone.utc)
        for e in pending:
            e.paused_at = now
        if pending:
            db.add(Activity(
                company_id=contact.company_id, contact_id=contact.id,
                user_id=rep.id if rep else None,
                activity_type="sequence_paused",
                content=f"Email sequence auto-paused — contact replied via SMS ({len(pending)} emails)",
            ))

        # Mark the company as 'replied' if the engagement justifies it
        company = (await db.execute(select(Company).where(Company.id == contact.company_id))).scalar_one_or_none()
        if company and company.status in ("sequencing", "contacted"):
            company.status = "replied"

        await db.commit()

    return Response(content="<Response/>", media_type="application/xml")


@router.post("/sms/status")
async def sms_status(request: Request):
    """Twilio delivery-status callback. Updates the Activity if delivery
    fails so the rep knows."""
    form = await request.form()
    message_sid = form.get("MessageSid", "")
    msg_status = form.get("MessageStatus", "")
    error_code = form.get("ErrorCode", "")
    if not message_sid:
        return Response(content="", media_type="application/xml")

    if msg_status in ("failed", "undelivered"):
        async with async_session() as db:
            # Find the matching sms_sent Activity by message_sid in metadata
            rows = (await db.execute(
                select(Activity).where(
                    Activity.activity_type == "sms_sent",
                    Activity.metadata_json.like(f'%"{message_sid}"%'),
                )
            )).scalars().all()
            for a in rows:
                meta = json.loads(a.metadata_json) if a.metadata_json else {}
                meta["delivery_status"] = msg_status
                if error_code:
                    meta["error_code"] = error_code
                a.metadata_json = json.dumps(meta)
                a.content = a.content + f" [DELIVERY {msg_status.upper()}]"
            await db.commit()
    return Response(content="", media_type="application/xml")
