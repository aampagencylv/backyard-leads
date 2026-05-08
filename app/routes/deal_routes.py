"""
Deal-level routes: CRUD on Deals + kanban-style pipeline view + forecast.
"""
from __future__ import annotations
from typing import Optional
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db
from app.models import User, Company, Deal, Activity
from app.auth import get_current_user

router = APIRouter(prefix="/api", tags=["deals"])


# Stage probability defaults
STAGE_PROBABILITY = {
    "in_sequence":  0,   # No value until they engage
    "prospecting": 10,
    "qualified":   25,
    "proposal":    50,
    "negotiation": 75,
    "closed_won":  100,
    "closed_lost": 0,
}

PIPELINE_STAGES = ["in_sequence", "prospecting", "qualified", "proposal", "negotiation", "closed_won", "closed_lost", "snoozed"]

# BMP Packages
BMP_PACKAGES = {
    "foundation": {"name": "Foundation", "monthly": 2000},
    "essential":  {"name": "Essential",  "monthly": 4000},
    "growth":     {"name": "Growth",     "monthly": 6000},
    "scale":      {"name": "Scale",      "monthly": 8000},
}


def recommend_package(employee_count: int = None) -> str:
    """Auto-recommend a package based on company size."""
    if not employee_count or employee_count <= 5:
        return "foundation"
    elif employee_count <= 15:
        return "essential"
    elif employee_count <= 50:
        return "growth"
    else:
        return "scale"


def package_monthly_value(package: str) -> float:
    """Get monthly price for a package."""
    return BMP_PACKAGES.get(package, {}).get("monthly", 0)


class CreateDealRequest(BaseModel):
    name: str
    value: Optional[float] = None
    stage: str = "prospecting"
    package: Optional[str] = None  # foundation, essential, growth, scale
    contract_months: int = 6  # 6 or 12
    expected_close_date: Optional[str] = None
    assigned_to: Optional[int] = None


class UpdateDealRequest(BaseModel):
    name: Optional[str] = None
    value: Optional[float] = None
    stage: Optional[str] = None
    package: Optional[str] = None
    contract_months: Optional[int] = None
    probability: Optional[int] = None
    expected_close_date: Optional[str] = None
    lost_reason: Optional[str] = None
    assigned_to: Optional[int] = None


@router.get("/packages")
async def list_packages(user: User = Depends(get_current_user)):
    """List available BMP packages with pricing."""
    return {
        "packages": [
            {"key": k, "name": v["name"], "monthly": v["monthly"],
             "annual": v["monthly"] * 12, "six_month": v["monthly"] * 6}
            for k, v in BMP_PACKAGES.items()
        ],
        "stage_probabilities": STAGE_PROBABILITY,
    }


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

    # Auto-recommend package if not specified
    pkg = req.package
    if not pkg and company.employee_count:
        pkg = recommend_package(company.employee_count)

    # Set value from package if not explicitly provided
    monthly = req.value
    if not monthly and pkg:
        monthly = package_monthly_value(pkg)

    deal = Deal(
        company_id=company_id,
        name=req.name,
        value=monthly,
        stage=req.stage,
        package=pkg,
        contract_months=req.contract_months,
        probability=STAGE_PROBABILITY.get(req.stage, 0),
        expected_close_date=close_date,
        assigned_to=req.assigned_to or user.id,
    )
    db.add(deal)

    pkg_label = BMP_PACKAGES.get(pkg, {}).get("name", pkg or "custom") if pkg else "custom"
    db.add(Activity(company_id=company_id, user_id=user.id, activity_type="deal_created",
                    content=f"Deal created: {req.name} — {pkg_label} ${monthly or 0:,.0f}/mo × {req.contract_months}mo"))
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
    from app.scoping import check_deal_access
    if not check_deal_access(deal, user):
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
    from app.scoping import check_deal_access
    if not check_deal_access(deal, user):
        raise HTTPException(status_code=404, detail="Deal not found")

    changes = []
    if req.name is not None and req.name != deal.name:
        changes.append(f"renamed to '{req.name}'")
        deal.name = req.name
    if req.value is not None and req.value != deal.value:
        changes.append(f"value: ${req.value:,.0f}")
        deal.value = req.value
    stage_changed_from = None
    stage_changed_to = None
    if req.stage is not None and req.stage != deal.stage:
        if req.stage not in PIPELINE_STAGES:
            raise HTTPException(status_code=400, detail=f"stage must be one of {PIPELINE_STAGES}")
        old = deal.stage
        deal.stage = req.stage
        deal.probability = STAGE_PROBABILITY.get(req.stage, 0)
        if req.stage in ("closed_won", "closed_lost"):
            deal.closed_at = datetime.now(timezone.utc)
        changes.append(f"stage: {old} → {req.stage}")
        stage_changed_from = old
        stage_changed_to = req.stage
    if req.probability is not None:
        deal.probability = max(0, min(100, req.probability))
    if req.expected_close_date is not None:
        deal.expected_close_date = _parse_date(req.expected_close_date)
    if req.lost_reason is not None:
        deal.lost_reason = req.lost_reason
    if req.assigned_to is not None:
        deal.assigned_to = req.assigned_to
    if req.package is not None and req.package != deal.package:
        deal.package = req.package
        # Auto-update value if they changed the package
        if req.value is None:
            deal.value = package_monthly_value(req.package)
            changes.append(f"package: {BMP_PACKAGES.get(req.package, {}).get('name', req.package)} (${deal.value:,.0f}/mo)")
        else:
            changes.append(f"package: {req.package}")
    if req.contract_months is not None:
        deal.contract_months = req.contract_months
        changes.append(f"contract: {req.contract_months} months")

    if changes:
        db.add(Activity(company_id=deal.company_id, user_id=user.id, deal_id=deal.id,
                        activity_type="deal_update", content="; ".join(changes)))
    await db.commit()
    await db.refresh(deal)

    # Outbound webhook on stage change — drives Slack alerts on
    # "deal moved to closed_won", Zapier integrations to update
    # external billing systems, etc.
    if stage_changed_to:
        try:
            from app.services.webhook_dispatch import dispatch_event
            await dispatch_event(db, "deal.stage_changed", {
                "deal_id": deal.id,
                "company_id": deal.company_id,
                "name": deal.name,
                "from_stage": stage_changed_from,
                "to_stage": stage_changed_to,
                "value": deal.value,
                "probability": deal.probability,
                "package": deal.package,
                "assigned_to": deal.assigned_to,
            })
        except Exception:
            pass

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
    from app.scoping import check_deal_access
    if not check_deal_access(deal, user):
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
    """Return deals grouped by stage for the kanban — scoped by user role."""
    from app.scoping import scope_deals
    query = scope_deals(select(Deal).where(Deal.pipeline == pipeline), user, owner)
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
    """Forecast scoped by user role. Reps see their forecast only."""
    from app.scoping import scope_deals
    open_stages = ("prospecting", "qualified", "proposal", "negotiation")
    query = scope_deals(
        select(Deal).where(Deal.pipeline == pipeline, Deal.stage.in_(open_stages)),
        user,
    )
    result = await db.execute(query)
    deals = result.scalars().all()

    # MRR calculations
    total_mrr = sum((d.value or 0) for d in deals)
    weighted_mrr = sum(((d.value or 0) * (d.probability or 0) / 100.0) for d in deals)
    total_arr = total_mrr * 12
    weighted_arr = weighted_mrr * 12

    # TCV = total contract value
    total_tcv = sum(((d.value or 0) * (d.contract_months or 6)) for d in deals)
    weighted_tcv = sum(((d.value or 0) * (d.contract_months or 6) * (d.probability or 0) / 100.0) for d in deals)

    # By stage
    by_stage = {}
    for stage in ("prospecting", "qualified", "proposal", "negotiation"):
        stage_deals = [d for d in deals if d.stage == stage]
        by_stage[stage] = {
            "count": len(stage_deals),
            "mrr": sum((d.value or 0) for d in stage_deals),
            "probability": STAGE_PROBABILITY.get(stage, 0),
        }

    # By package
    by_package = {}
    for pkg_key, pkg_info in BMP_PACKAGES.items():
        pkg_deals = [d for d in deals if d.package == pkg_key]
        by_package[pkg_key] = {
            "name": pkg_info["name"],
            "count": len(pkg_deals),
            "mrr": sum((d.value or 0) for d in pkg_deals),
        }

    won = (await db.execute(
        select(Deal).where(Deal.pipeline == pipeline, Deal.stage == "closed_won")
    )).scalars().all()
    won_mrr = sum((d.value or 0) for d in won)

    return {
        "pipeline": pipeline,
        "open_deal_count": len(deals),
        "potential_mrr": round(total_mrr, 2),
        "potential_arr": round(total_arr, 2),
        "weighted_mrr": round(weighted_mrr, 2),
        "weighted_arr": round(weighted_arr, 2),
        "potential_tcv": round(total_tcv, 2),
        "weighted_tcv": round(weighted_tcv, 2),
        "by_stage": by_stage,
        "by_package": by_package,
        "closed_won_count": len(won),
        "closed_won_mrr": won_mrr,
        "closed_won_arr": won_mrr * 12,
    }


# ============================================================
# Helpers
# ============================================================

def _deal_to_dict(d: Deal) -> dict:
    monthly = d.value or 0
    contract = d.contract_months or 6
    pkg = d.package or ""
    pkg_label = BMP_PACKAGES.get(pkg, {}).get("name", pkg.title()) if pkg else "Custom"
    weighted = monthly * (d.probability or 0) / 100
    return {
        "id": d.id,
        "company_id": d.company_id,
        "name": d.name,
        "value": d.value,
        "stage": d.stage,
        "pipeline": d.pipeline,
        "probability": d.probability,
        "package": pkg,
        "package_label": pkg_label,
        "contract_months": contract,
        "mrr": monthly,
        "arr": monthly * 12,
        "tcv": monthly * contract,  # Total contract value
        "weighted_mrr": weighted,
        "expected_close_date": d.expected_close_date.isoformat() if d.expected_close_date else None,
        "closed_at": d.closed_at.isoformat() if d.closed_at else None,
        "lost_reason": d.lost_reason,
        "assigned_to": d.assigned_to,
        "snoozed_until": d.snoozed_until.isoformat() if d.snoozed_until else None,
        "snooze_reason": d.snooze_reason,
        "stage_before_snooze": d.stage_before_snooze,
        "is_snoozed": d.stage == "snoozed",
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


# ============================================================
# Snooze / Reactivation
# ============================================================

class SnoozeDealRequest(BaseModel):
    days: Optional[int] = None  # 30, 60, 90
    until_date: Optional[str] = None  # ISO date
    reason: str = ""
    pause_sequence: bool = True
    auto_task_on_wake: bool = True
    auto_new_sequence: bool = False


@router.post("/deals/{deal_id}/snooze")
async def snooze_deal(
    deal_id: int,
    req: SnoozeDealRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Snooze a deal — pause sequence, set wake date, log reason."""
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Deal not found")
    from app.scoping import check_deal_access
    if not check_deal_access(deal, user):
        raise HTTPException(status_code=404, detail="Deal not found")

    # Calculate wake date
    if req.until_date:
        wake = _parse_date(req.until_date)
    elif req.days:
        wake = datetime.now(timezone.utc) + timedelta(days=req.days)
    else:
        wake = datetime.now(timezone.utc) + timedelta(days=30)

    # Save current stage so we can restore on wake
    deal.stage_before_snooze = deal.stage
    deal.snoozed_until = wake
    deal.snooze_reason = req.reason
    deal.stage = "snoozed"
    deal.probability = 0

    # Pause active sequences for all contacts at this company
    if req.pause_sequence:
        from app.models import GeneratedEmail, Contact
        contacts = (await db.execute(
            select(Contact.id).where(Contact.company_id == deal.company_id)
        )).scalars().all()
        if contacts:
            from sqlalchemy import update
            await db.execute(
                update(GeneratedEmail)
                .where(
                    GeneratedEmail.contact_id.in_(contacts),
                    GeneratedEmail.is_sent == False,
                    GeneratedEmail.paused_at.is_(None),
                )
                .values(paused_at=datetime.now(timezone.utc))
            )

    # Log
    db.add(Activity(
        company_id=deal.company_id, user_id=user.id, deal_id=deal.id,
        activity_type="deal_snoozed",
        content=f"Snoozed until {wake.strftime('%b %d, %Y')}: {req.reason}" if req.reason else f"Snoozed until {wake.strftime('%b %d, %Y')}",
    ))

    await db.commit()

    return {
        "id": deal.id,
        "stage": deal.stage,
        "snoozed_until": deal.snoozed_until.isoformat() if deal.snoozed_until else None,
        "snooze_reason": deal.snooze_reason,
        "stage_before_snooze": deal.stage_before_snooze,
    }


@router.post("/deals/{deal_id}/wake")
async def wake_deal(
    deal_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Manually wake a snoozed deal before its scheduled date."""
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Deal not found")

    restore_stage = deal.stage_before_snooze or "prospecting"
    deal.stage = restore_stage
    deal.probability = STAGE_PROBABILITY.get(restore_stage, 10)
    deal.snoozed_until = None
    deal.stage_before_snooze = None

    # Re-assign package value if it was zeroed
    if deal.value == 0 and deal.package:
        deal.value = package_monthly_value(deal.package)

    db.add(Activity(
        company_id=deal.company_id, user_id=user.id, deal_id=deal.id,
        activity_type="deal_woken",
        content=f"Deal reactivated — restored to {restore_stage}",
    ))

    await db.commit()
    return _deal_to_dict(deal)
