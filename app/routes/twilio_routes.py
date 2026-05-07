"""
Twilio admin routes — Phase 1: number management only.

All endpoints require role='admin'. Phase 2 (click-to-call) and beyond
add the dialer + webhook routes.
"""
from __future__ import annotations
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db
from app.models import User
from app.auth import get_current_user
from app.runtime_config import get_twilio_credentials
from app.services.twilio_voice import (
    search_available_numbers,
    buy_number,
    list_owned_numbers,
    release_number,
    number_to_dict,
    TwilioError,
)

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
