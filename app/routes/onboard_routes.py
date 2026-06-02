"""
Tenant onboarding wizard — backend state machine.

The wizard UI advances a tenant through:
  pending → brand → phone → email → a2p → team → plan → done

Steps "brand", "team", and "plan" are required. Steps "phone", "email",
and "a2p" are usable but skippable — a tenant can enter the app on
email-only and add SMS/voice later.

These routes are tenant-scoped (the active admin user's tenant) and
gated on role=admin or super_admin. The platform admin creating a
tenant externally (POST /api/admin/tenants) is a separate flow; once
that's done, the first super_admin in that tenant uses this wizard.
"""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, require_admin
from app.models import Tenant, User
from app.tenancy import get_tenant_db, get_current_tenant_id

router = APIRouter(prefix="/api/onboard", tags=["onboard"])

# Wizard step machine. Ordered for next/prev navigation.
ORDER = ("pending", "brand", "phone", "email", "a2p", "team", "plan", "done")
VALID = set(ORDER)


class StatusOut(BaseModel):
    tenant_id: int
    tenant_name: str
    onboarding_step: str
    next_step: Optional[str] = None
    is_complete: bool


class AdvanceIn(BaseModel):
    step: str = Field(description="Target step; must be one of the valid step names.")


def _next_step(current: str) -> Optional[str]:
    try:
        i = ORDER.index(current)
    except ValueError:
        return None
    if i + 1 >= len(ORDER):
        return None
    return ORDER[i + 1]


@router.get("/status", response_model=StatusOut)
async def status(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_admin),
    tenant_id: int = Depends(get_current_tenant_id),
):
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="tenant not found")
    step = tenant.onboarding_step or "pending"
    nxt = _next_step(step)
    return StatusOut(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        onboarding_step=step,
        next_step=nxt,
        is_complete=(step == "done"),
    )


@router.post("/advance", response_model=StatusOut)
async def advance(
    req: AdvanceIn,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_admin),
    tenant_id: int = Depends(get_current_tenant_id),
):
    if req.step not in VALID:
        raise HTTPException(status_code=400,
                            detail=f"step must be one of {sorted(VALID)}")
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="tenant not found")
    tenant.onboarding_step = req.step
    await db.commit()
    nxt = _next_step(req.step)
    return StatusOut(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        onboarding_step=tenant.onboarding_step,
        next_step=nxt,
        is_complete=(req.step == "done"),
    )
