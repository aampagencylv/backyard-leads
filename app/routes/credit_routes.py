"""
Credit ledger + admin COGS endpoints.

Two layers:
  /api/credits/*       — current user's credit usage (any logged-in user)
  /api/admin/cogs/*    — platform cost-of-goods (admin only)

Shim mode: nothing enforced. These are read-only views over the
credit_ledger rows that the meter writes.
"""
from __future__ import annotations
from typing import Optional
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.auth import get_current_user, require_admin
from app.models import User, CreditLedger
from app.services.credit_meter import RATE_CARD


router = APIRouter(prefix="/api", tags=["credits"])


def _window_start(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


# ============================================================
# User-facing — current usage + burn rate
# ============================================================

@router.get("/credits/me")
async def my_credits(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the current user's credit usage over the last `days`.
    Shim mode: no balance, just spend totals."""
    since = _window_start(days)

    total_credits = (await db.execute(
        select(func.coalesce(func.sum(CreditLedger.credits_debited), 0))
        .where(CreditLedger.user_id == user.id, CreditLedger.created_at >= since)
    )).scalar()
    total_actions = (await db.execute(
        select(func.count(CreditLedger.id))
        .where(CreditLedger.user_id == user.id, CreditLedger.created_at >= since)
    )).scalar()

    by_action_rows = (await db.execute(
        select(
            CreditLedger.action_type,
            func.count(CreditLedger.id),
            func.coalesce(func.sum(CreditLedger.credits_debited), 0),
        )
        .where(CreditLedger.user_id == user.id, CreditLedger.created_at >= since)
        .group_by(CreditLedger.action_type)
        .order_by(func.sum(CreditLedger.credits_debited).desc())
    )).all()

    by_action = [
        {"action_type": r[0], "count": int(r[1]), "credits": int(r[2])}
        for r in by_action_rows
    ]

    return {
        "user_id": user.id,
        "window_days": days,
        "total_credits_used": int(total_credits or 0),
        "total_actions": int(total_actions or 0),
        "burn_per_day": round((total_credits or 0) / max(days, 1), 1),
        "by_action": by_action,
    }


@router.get("/credits/rate-card")
async def rate_card(_user: User = Depends(get_current_user)):
    """Return the live action rate card (credits + raw cost).
    Customers don't see raw_cost_usd — but we expose credits for transparency."""
    return {
        "rates": [
            {
                "action_type": k,
                "credits": v.credits,
                "vendor": v.vendor,
            }
            for k, v in RATE_CARD.items()
        ],
    }


# ============================================================
# Admin — platform COGS (raw vendor spend, margin per user)
# ============================================================

@router.get("/admin/cogs/summary")
async def cogs_summary(
    days: int = Query(30, ge=1, le=365),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Platform-wide cost-of-goods over the last `days`.
    Admin/super_admin only. Shows raw vendor spend, broken out by vendor + action."""
    since = _window_start(days)

    totals = (await db.execute(
        select(
            func.count(CreditLedger.id),
            func.coalesce(func.sum(CreditLedger.credits_debited), 0),
            func.coalesce(func.sum(CreditLedger.raw_cost_usd), 0.0),
        ).where(CreditLedger.created_at >= since)
    )).one()

    by_vendor_rows = (await db.execute(
        select(
            CreditLedger.vendor,
            func.count(CreditLedger.id),
            func.coalesce(func.sum(CreditLedger.raw_cost_usd), 0.0),
            func.coalesce(func.sum(CreditLedger.credits_debited), 0),
        )
        .where(CreditLedger.created_at >= since)
        .group_by(CreditLedger.vendor)
        .order_by(func.sum(CreditLedger.raw_cost_usd).desc())
    )).all()

    by_action_rows = (await db.execute(
        select(
            CreditLedger.action_type,
            func.count(CreditLedger.id),
            func.coalesce(func.sum(CreditLedger.raw_cost_usd), 0.0),
            func.coalesce(func.sum(CreditLedger.credits_debited), 0),
        )
        .where(CreditLedger.created_at >= since)
        .group_by(CreditLedger.action_type)
        .order_by(func.sum(CreditLedger.raw_cost_usd).desc())
    )).all()

    return {
        "window_days": days,
        "total_actions": int(totals[0] or 0),
        "total_credits": int(totals[1] or 0),
        "total_raw_cost_usd": round(float(totals[2] or 0), 4),
        "by_vendor": [
            {"vendor": r[0] or "unknown", "count": int(r[1]),
             "raw_cost_usd": round(float(r[2]), 4), "credits": int(r[3])}
            for r in by_vendor_rows
        ],
        "by_action": [
            {"action_type": r[0], "count": int(r[1]),
             "raw_cost_usd": round(float(r[2]), 4), "credits": int(r[3])}
            for r in by_action_rows
        ],
    }


@router.get("/admin/cogs/by-user")
async def cogs_by_user(
    days: int = Query(30, ge=1, le=365),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Per-user COGS breakdown — what each rep is costing the platform.
    Helps spot outliers (rep burning credits without booking meetings)."""
    since = _window_start(days)

    rows = (await db.execute(
        select(
            CreditLedger.user_id,
            func.count(CreditLedger.id),
            func.coalesce(func.sum(CreditLedger.credits_debited), 0),
            func.coalesce(func.sum(CreditLedger.raw_cost_usd), 0.0),
        )
        .where(CreditLedger.created_at >= since)
        .group_by(CreditLedger.user_id)
        .order_by(func.sum(CreditLedger.raw_cost_usd).desc())
    )).all()

    # Resolve user emails — small N (handful of reps), one query.
    user_ids = [r[0] for r in rows if r[0] is not None]
    user_map = {}
    if user_ids:
        users = (await db.execute(
            select(User.id, User.email, User.first_name, User.last_name)
            .where(User.id.in_(user_ids))
        )).all()
        user_map = {u[0]: {"email": u[1], "name": f"{u[2]} {u[3]}".strip()} for u in users}

    return {
        "window_days": days,
        "rows": [
            {
                "user_id": r[0],
                "user_email": user_map.get(r[0], {}).get("email", "(system)" if r[0] is None else "(unknown)"),
                "user_name": user_map.get(r[0], {}).get("name", ""),
                "actions": int(r[1]),
                "credits_debited": int(r[2]),
                "raw_cost_usd": round(float(r[3]), 4),
            }
            for r in rows
        ],
    }


@router.get("/admin/cogs/recent")
async def cogs_recent(
    limit: int = Query(50, ge=1, le=500),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Tail of the credit ledger — last N rows. For debugging / spot-checking."""
    rows = (await db.execute(
        select(CreditLedger).order_by(CreditLedger.id.desc()).limit(limit)
    )).scalars().all()
    return {
        "rows": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "action_type": r.action_type,
                "action_ref": r.action_ref,
                "vendor": r.vendor,
                "credits_debited": r.credits_debited,
                "raw_cost_usd": r.raw_cost_usd,
                "idempotency_key": r.idempotency_key,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
