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


# ============================================================
# Call activity reporting
# ============================================================

@router.get("/dashboard/calls")
async def dashboard_calls(
    days: int = 7,
    user_id: Optional[int] = None,  # admin can scope to a specific rep; null = all (admin) or self (rep)
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Per-rep call activity for the last N days.
    Reps see only their own data; admins see everyone (or scope to one rep).
    """
    days = max(1, min(days, 90))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Authorization scope
    if user.role != "admin":
        scope_user_ids = [user.id]
    elif user_id is not None:
        scope_user_ids = [user_id]
    else:
        scope_user_ids = None  # all reps

    query = select(Activity).where(
        Activity.activity_type.in_(("call", "voicemail")),
        Activity.created_at >= cutoff,
    )
    if scope_user_ids:
        query = query.where(Activity.user_id.in_(scope_user_ids))

    rows = (await db.execute(query)).scalars().all()

    # Aggregate
    total = len(rows)
    outbound = sum(1 for a in rows if a.call_direction == "outbound")
    inbound = sum(1 for a in rows if a.call_direction == "inbound")
    by_outcome = {}
    talk_seconds = 0
    by_day: dict[str, dict] = {}
    by_rep: dict[int, dict] = {}

    for a in rows:
        oc = a.call_outcome or "unknown"
        by_outcome[oc] = by_outcome.get(oc, 0) + 1

        if a.call_duration_seconds:
            talk_seconds += a.call_duration_seconds

        # Day buckets (UTC)
        day = (a.created_at.date().isoformat() if a.created_at else "unknown")
        d = by_day.setdefault(day, {"date": day, "calls": 0, "connected": 0, "talk_seconds": 0})
        d["calls"] += 1
        if a.call_outcome == "connected":
            d["connected"] += 1
        d["talk_seconds"] += a.call_duration_seconds or 0

        if a.user_id:
            r = by_rep.setdefault(a.user_id, {
                "user_id": a.user_id, "name": "", "calls": 0, "connected": 0,
                "voicemail": 0, "no_answer": 0, "talk_seconds": 0,
            })
            r["calls"] += 1
            if a.call_outcome == "connected":
                r["connected"] += 1
            elif a.call_outcome == "voicemail":
                r["voicemail"] += 1
            elif a.call_outcome == "no_answer":
                r["no_answer"] += 1
            r["talk_seconds"] += a.call_duration_seconds or 0

    # Resolve rep names
    if by_rep:
        u_rows = (await db.execute(select(User).where(User.id.in_(by_rep.keys())))).scalars().all()
        for u in u_rows:
            if u.id in by_rep:
                by_rep[u.id]["name"] = u.full_name or u.email

    connected = by_outcome.get("connected", 0)
    connect_rate = round(connected / total, 4) if total else 0.0
    avg_talk = round(talk_seconds / max(connected, 1)) if connected else 0  # avg of CONNECTED calls only

    # Sort outputs
    by_day_sorted = sorted(by_day.values(), key=lambda d: d["date"])
    by_rep_sorted = sorted(by_rep.values(), key=lambda r: -r["calls"])

    return {
        "days": days,
        "summary": {
            "total_calls": total,
            "outbound": outbound,
            "inbound": inbound,
            "connected": connected,
            "connect_rate": connect_rate,
            "voicemail": by_outcome.get("voicemail", 0),
            "no_answer": by_outcome.get("no_answer", 0),
            "talk_seconds": talk_seconds,
            "talk_hours": round(talk_seconds / 3600, 2),
            "avg_talk_seconds": avg_talk,  # avg of connected calls
        },
        "by_outcome": by_outcome,
        "by_day": by_day_sorted,
        "by_rep": by_rep_sorted,
    }
