"""
Deal-level routes: CRUD on Deals + kanban-style pipeline view + forecast.
"""
from __future__ import annotations
from typing import Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db
from app.models import User, Company, Deal, Activity
from app.auth import get_current_user

router = APIRouter(prefix="/api", tags=["deals"])


# Stage probability defaults (overridden if explicitly set on the deal)
STAGE_PROBABILITY = {
    "prospecting": 5,
    "qualified":   15,
    "proposal":    35,
    "negotiation": 65,
    "closed_won":  100,
    "closed_lost": 0,
}

PIPELINE_STAGES = ["prospecting", "qualified", "proposal", "negotiation", "closed_won", "closed_lost"]


class CreateDealRequest(BaseModel):
    name: str
    value: Optional[float] = None
    stage: str = "prospecting"
    expected_close_date: Optional[str] = None
    assigned_to: Optional[int] = None


class UpdateDealRequest(BaseModel):
    name: Optional[str] = None
    value: Optional[float] = None
    stage: Optional[str] = None
    probability: Optional[int] = None
    expected_close_date: Optional[str] = None
    lost_reason: Optional[str] = None
    assigned_to: Optional[int] = None


@router.get("/companies/{company_id}/deals")
async def list_company_deals(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Deal).where(Deal.company_id == company_id).order_by(Deal.created_at.desc())
    )
    return [_deal_to_dict(d) for d in result.scalars().all()]


@router.post("/companies/{company_id}/deals")
async def create_deal(
    company_id: int,
    req: CreateDealRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if req.stage not in PIPELINE_STAGES:
        raise HTTPException(status_code=400, detail=f"stage must be one of {PIPELINE_STAGES}")
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    close_date = _parse_date(req.expected_close_date)
    deal = Deal(
        company_id=company_id,
        name=req.name,
        value=req.value,
        stage=req.stage,
        probability=STAGE_PROBABILITY.get(req.stage, 0),
        expected_close_date=close_date,
        assigned_to=req.assigned_to or user.id,
    )
    db.add(deal)
    db.add(Activity(company_id=company_id, user_id=user.id, activity_type="deal_created",
                    content=f"Deal created: {req.name} ({req.stage})"))
    await db.commit()
    await db.refresh(deal)
    return _deal_to_dict(deal)


@router.get("/deals/{deal_id}")
async def get_deal(
    deal_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    return _deal_to_dict(deal)


@router.patch("/deals/{deal_id}")
async def update_deal(
    deal_id: int,
    req: UpdateDealRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")

    changes = []
    if req.name is not None and req.name != deal.name:
        changes.append(f"renamed to '{req.name}'")
        deal.name = req.name
    if req.value is not None and req.value != deal.value:
        changes.append(f"value: ${req.value:,.0f}")
        deal.value = req.value
    if req.stage is not None and req.stage != deal.stage:
        if req.stage not in PIPELINE_STAGES:
            raise HTTPException(status_code=400, detail=f"stage must be one of {PIPELINE_STAGES}")
        old = deal.stage
        deal.stage = req.stage
        deal.probability = STAGE_PROBABILITY.get(req.stage, 0)
        if req.stage in ("closed_won", "closed_lost"):
            deal.closed_at = datetime.now(timezone.utc)
        changes.append(f"stage: {old} → {req.stage}")
    if req.probability is not None:
        deal.probability = max(0, min(100, req.probability))
    if req.expected_close_date is not None:
        deal.expected_close_date = _parse_date(req.expected_close_date)
    if req.lost_reason is not None:
        deal.lost_reason = req.lost_reason
    if req.assigned_to is not None:
        deal.assigned_to = req.assigned_to

    if changes:
        db.add(Activity(company_id=deal.company_id, user_id=user.id, deal_id=deal.id,
                        activity_type="deal_update", content="; ".join(changes)))
    await db.commit()
    await db.refresh(deal)
    return _deal_to_dict(deal)


@router.delete("/deals/{deal_id}")
async def delete_deal(
    deal_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    cid = deal.company_id
    await db.delete(deal)
    db.add(Activity(company_id=cid, user_id=user.id, activity_type="deal_deleted",
                    content=f"Deal deleted: {deal.name}"))
    await db.commit()
    return {"deleted": True}


# ============================================================
# Pipeline kanban data + forecast
# ============================================================

@router.get("/pipeline")
async def pipeline_view(
    pipeline: str = "default",
    owner: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return deals grouped by stage for the kanban — open stages only by default."""
    query = select(Deal).where(Deal.pipeline == pipeline)
    if owner:
        query = query.where(Deal.assigned_to == owner)
    result = await db.execute(query.order_by(Deal.updated_at.desc()))
    deals = result.scalars().all()

    # Pull companies in one query
    company_ids = {d.company_id for d in deals}
    companies = {}
    if company_ids:
        c_result = await db.execute(select(Company).where(Company.id.in_(company_ids)))
        companies = {c.id: c for c in c_result.scalars().all()}

    columns = {stage: [] for stage in PIPELINE_STAGES}
    for d in deals:
        c = companies.get(d.company_id)
        columns[d.stage].append({
            "id": d.id,
            "name": d.name,
            "value": d.value,
            "probability": d.probability,
            "expected_close_date": d.expected_close_date.isoformat() if d.expected_close_date else None,
            "lost_reason": d.lost_reason,
            "assigned_to": d.assigned_to,
            "company": {
                "id": c.id if c else d.company_id,
                "name": c.name if c else "(unknown)",
                "city": c.city if c else None,
                "state": c.state if c else None,
                "website": c.website if c else None,
            },
            "updated_at": d.updated_at.isoformat() if d.updated_at else None,
        })

    totals = {
        stage: {
            "count": len(items),
            "value": sum((i["value"] or 0) for i in items),
        }
        for stage, items in columns.items()
    }

    return {
        "pipeline": pipeline,
        "stages": PIPELINE_STAGES,
        "columns": columns,
        "totals": totals,
    }


@router.get("/forecast")
async def forecast(
    pipeline: str = "default",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Simple forecast: weighted sum of open deals. value × probability / 100."""
    open_stages = ("prospecting", "qualified", "proposal", "negotiation")
    result = await db.execute(
        select(Deal).where(Deal.pipeline == pipeline, Deal.stage.in_(open_stages))
    )
    deals = result.scalars().all()

    total_pipeline = sum((d.value or 0) for d in deals)
    weighted = sum(((d.value or 0) * (d.probability or 0) / 100.0) for d in deals)

    won = (await db.execute(
        select(Deal).where(Deal.pipeline == pipeline, Deal.stage == "closed_won")
    )).scalars().all()
    won_total = sum((d.value or 0) for d in won)

    return {
        "pipeline": pipeline,
        "open_deal_count": len(deals),
        "open_pipeline_value": total_pipeline,
        "weighted_forecast": round(weighted, 2),
        "closed_won_count": len(won),
        "closed_won_value": won_total,
    }


# ============================================================
# Helpers
# ============================================================

def _deal_to_dict(d: Deal) -> dict:
    return {
        "id": d.id,
        "company_id": d.company_id,
        "name": d.name,
        "value": d.value,
        "stage": d.stage,
        "pipeline": d.pipeline,
        "probability": d.probability,
        "expected_close_date": d.expected_close_date.isoformat() if d.expected_close_date else None,
        "closed_at": d.closed_at.isoformat() if d.closed_at else None,
        "lost_reason": d.lost_reason,
        "assigned_to": d.assigned_to,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
