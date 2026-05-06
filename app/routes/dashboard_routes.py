"""
Dashboard + activity feed endpoints — power the home page.

Single endpoint /api/dashboard returns all 5 zones of the dashboard:
KPIs, today's focus, hot leads, pipeline-by-stage, and activity feed.

A separate /api/activity/feed exists for the standalone feed pane.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from math import exp
from typing import Optional
from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_

from app.database import get_db
from app.models import User, Company, Contact, Deal, GeneratedEmail, Activity, Task
from app.auth import get_current_user

router = APIRouter(prefix="/api", tags=["dashboard"])


# Engagement event weights + decay
ENGAGEMENT_WEIGHTS = {
    "email_opened":  1,
    "email_clicked": 5,
    "email_replied": 20,
}
ENGAGEMENT_DECAY_HALFLIFE_DAYS = 14
ENGAGEMENT_LOOKBACK_DAYS = 30
HOT_LEAD_THRESHOLD = 3  # min score to qualify as "hot"

OPEN_DEAL_STAGES = ("prospecting", "qualified", "proposal", "negotiation")
PIPELINE_STAGES = list(OPEN_DEAL_STAGES) + ["closed_won", "closed_lost"]
STALE_DEAL_DAYS = 14


def _decay_weight(age_days: float) -> float:
    """Half-life decay: an event from 14 days ago counts as 0.5x today's event."""
    return 0.5 ** (age_days / ENGAGEMENT_DECAY_HALFLIFE_DAYS)


@router.get("/dashboard")
async def get_dashboard(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    cutoff_engagement = now - timedelta(days=ENGAGEMENT_LOOKBACK_DAYS)
    cutoff_stale = now - timedelta(days=STALE_DEAL_DAYS)

    # ---------- Pipeline-by-stage ----------
    deals_open_or_done = (await db.execute(
        select(Deal).where(Deal.pipeline == "default")
    )).scalars().all()

    by_stage: dict[str, dict] = {s: {"count": 0, "value": 0.0} for s in PIPELINE_STAGES}
    pipeline_value = 0.0
    weighted_forecast = 0.0
    for d in deals_open_or_done:
        stage = d.stage if d.stage in by_stage else "prospecting"
        v = d.value or 0
        by_stage[stage]["count"] += 1
        by_stage[stage]["value"] += v
        if stage in OPEN_DEAL_STAGES:
            pipeline_value += v
            weighted_forecast += v * (d.probability or 0) / 100

    # Won this month
    won_mtd_value = sum(
        (d.value or 0) for d in deals_open_or_done
        if d.stage == "closed_won" and d.closed_at and d.closed_at >= month_start
    )
    won_mtd_count = sum(
        1 for d in deals_open_or_done
        if d.stage == "closed_won" and d.closed_at and d.closed_at >= month_start
    )

    # ---------- Engagement events for hot-lead scoring ----------
    eng_rows = (await db.execute(
        select(Activity).where(
            Activity.activity_type.in_(list(ENGAGEMENT_WEIGHTS.keys())),
            Activity.created_at >= cutoff_engagement,
        ).order_by(Activity.created_at.desc())
    )).scalars().all()

    # Aggregate score per company (also remember the most recent signal type per company)
    company_score: dict[int, float] = defaultdict(float)
    company_signals: dict[int, list[dict]] = defaultdict(list)
    for a in eng_rows:
        age = (now - a.created_at).total_seconds() / 86400 if a.created_at else 0
        weight = ENGAGEMENT_WEIGHTS.get(a.activity_type, 0)
        company_score[a.company_id] += weight * _decay_weight(age)
        if len(company_signals[a.company_id]) < 3:  # keep top 3 most recent signals
            company_signals[a.company_id].append({
                "type": a.activity_type, "at": a.created_at.isoformat() if a.created_at else None,
                "contact_id": a.contact_id,
            })

    # Hot leads: score >= threshold, sorted desc
    hot_company_ids = sorted(
        [cid for cid, score in company_score.items() if score >= HOT_LEAD_THRESHOLD],
        key=lambda cid: -company_score[cid],
    )[:10]

    hot_leads = []
    if hot_company_ids:
        rows = (await db.execute(
            select(Company).where(Company.id.in_(hot_company_ids))
        )).scalars().all()
        by_id = {c.id: c for c in rows}
        for cid in hot_company_ids:
            c = by_id.get(cid)
            if not c:
                continue
            hot_leads.append({
                "id": c.id,
                "name": c.name,
                "status": c.status,
                "engagement_score": round(company_score[cid], 1),
                "signals": company_signals[cid],
            })

    # ---------- Today's tasks (mine) ----------
    todays_tasks_rows = (await db.execute(
        select(Task, Company.name)
        .join(Company, Task.company_id == Company.id)
        .where(
            Task.user_id == user.id,
            Task.completed == False,
            Task.due_date >= today,
            Task.due_date < tomorrow,
        )
        .order_by(Task.due_date)
    )).all()
    todays_tasks = [
        {"id": t.id, "description": t.description,
         "company_id": t.company_id, "company_name": cname,
         "due_date": t.due_date.isoformat() if t.due_date else None}
        for t, cname in todays_tasks_rows
    ]

    # Total open tasks (for KPI)
    open_tasks_count = (await db.execute(
        select(func.count()).select_from(Task)
        .where(Task.user_id == user.id, Task.completed == False)
    )).scalar() or 0

    # ---------- Sequences ready to send (scheduled <= now, not yet sent, not paused) ----------
    queued_rows = (await db.execute(
        select(GeneratedEmail, Contact, Company.name)
        .join(Contact, GeneratedEmail.contact_id == Contact.id)
        .join(Company, GeneratedEmail.company_id == Company.id)
        .where(
            GeneratedEmail.is_sent == False,
            GeneratedEmail.paused_at.is_(None),
            GeneratedEmail.scheduled_send_at <= now,
            Contact.unsubscribed_at.is_(None),
        )
        .order_by(GeneratedEmail.scheduled_send_at)
        .limit(10)
    )).all()
    queued_emails = [
        {
            "email_id": e.id,
            "subject": e.subject,
            "company_id": e.company_id,
            "company_name": cname,
            "contact_id": c.id,
            "contact_name": c.full_name or c.email,
            "scheduled_send_at": e.scheduled_send_at.isoformat() if e.scheduled_send_at else None,
        }
        for e, c, cname in queued_rows
    ]

    # ---------- Stuck deals ----------
    stuck_rows = (await db.execute(
        select(Deal, Company.name)
        .join(Company, Deal.company_id == Company.id)
        .where(
            Deal.stage.in_(OPEN_DEAL_STAGES),
            Deal.updated_at <= cutoff_stale,
        )
        .order_by(Deal.updated_at)
        .limit(10)
    )).all()
    stuck_deals = [
        {
            "id": d.id,
            "name": d.name,
            "stage": d.stage,
            "value": d.value,
            "probability": d.probability,
            "company_id": d.company_id,
            "company_name": cname,
            "days_stuck": (now - d.updated_at).days if d.updated_at else 0,
        }
        for d, cname in stuck_rows
    ]

    # ---------- Sent-this-week count ----------
    sent_this_week = (await db.execute(
        select(func.count()).select_from(GeneratedEmail)
        .where(GeneratedEmail.is_sent == True, GeneratedEmail.sent_at >= week_start)
    )).scalar() or 0

    # ---------- Activity feed (last 20, cross-company) ----------
    feed_rows = (await db.execute(
        select(Activity, Company.name, User.first_name, User.last_name)
        .join(Company, Activity.company_id == Company.id)
        .outerjoin(User, Activity.user_id == User.id)
        .order_by(Activity.created_at.desc())
        .limit(20)
    )).all()
    activity_feed = [
        {
            "id": a.id,
            "type": a.activity_type,
            "content": a.content,
            "company_id": a.company_id,
            "company_name": cname,
            "user_name": f"{ufirst} {ulast}".strip() if ufirst else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a, cname, ufirst, ulast in feed_rows
    ]

    return {
        "kpis": {
            "pipeline_value": round(pipeline_value, 2),
            "weighted_forecast": round(weighted_forecast, 2),
            "won_mtd_value": round(won_mtd_value, 2),
            "won_mtd_count": won_mtd_count,
            "hot_leads_count": len(hot_leads),
            "open_tasks_count": open_tasks_count,
            "sent_this_week": sent_this_week,
        },
        "todays_tasks": todays_tasks,
        "queued_emails": queued_emails,
        "stuck_deals": stuck_deals,
        "hot_leads": hot_leads,
        "pipeline_by_stage": [
            {"stage": s, "count": by_stage[s]["count"], "value": round(by_stage[s]["value"], 2)}
            for s in PIPELINE_STAGES
        ],
        "activity_feed": activity_feed,
    }


@router.get("/activity/feed")
async def activity_feed(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(
        select(Activity, Company.name, User.first_name, User.last_name)
        .join(Company, Activity.company_id == Company.id)
        .outerjoin(User, Activity.user_id == User.id)
        .order_by(Activity.created_at.desc())
        .limit(min(limit, 200))
    )).all()
    return [
        {
            "id": a.id,
            "type": a.activity_type,
            "content": a.content,
            "company_id": a.company_id,
            "company_name": cname,
            "contact_id": a.contact_id,
            "deal_id": a.deal_id,
            "user_name": f"{ufirst} {ulast}".strip() if ufirst else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a, cname, ufirst, ulast in rows
    ]
