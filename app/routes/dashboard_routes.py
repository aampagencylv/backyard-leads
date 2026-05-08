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


def _aware(dt):
    """SQLite drops tzinfo on round-trip — column DateTime values come back
    naive even though we wrote them as tz-aware UTC. This helper coerces a
    naive value back to UTC-aware so it can be compared/subtracted with
    tz-aware now()."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


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
    from app.scoping import scope_deals, scope_companies
    deal_query = scope_deals(select(Deal).where(Deal.pipeline == "default"), user)
    deals_open_or_done = (await db.execute(deal_query)).scalars().all()

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
        if d.stage == "closed_won" and d.closed_at and _aware(d.closed_at) >= month_start
    )
    won_mtd_count = sum(
        1 for d in deals_open_or_done
        if d.stage == "closed_won" and d.closed_at and _aware(d.closed_at) >= month_start
    )

    # ---------- Hot leads (v2 scorer: fit × intent) ----------
    # The Company.lead_score column is the source of truth — written by
    # app/services/lead_scorer.py. To keep the widget accurate even when a
    # company hasn't been viewed in >1h, run a freshness sweep first:
    # find any company with engagement Activity newer than its cached
    # lead_score_updated_at and recompute those before the SELECT.
    from app.services.lead_scorer import get_or_recompute, _tier
    stale_q = (await db.execute(
        select(Activity.company_id, func.max(Activity.created_at))
        .where(
            Activity.activity_type.in_((
                "email_opened", "email_clicked", "email_replied",
                "form_submit", "tel_click", "mailto_click",
                "outbound_click", "pageview", "hot_lead", "meeting_booked",
            )),
            Activity.created_at >= cutoff_engagement,
        )
        .group_by(Activity.company_id)
    )).all()

    # Map company_id → most-recent engagement activity timestamp
    latest_by_co = {row[0]: _aware(row[1]) for row in stale_q if row[0] is not None}

    # Pull the affected companies + companies with already-non-zero scores
    sweep_ids = list(latest_by_co.keys())
    if sweep_ids:
        sweep_rows = (await db.execute(
            select(Company).where(Company.id.in_(sweep_ids))
        )).scalars().all()
        for c in sweep_rows[:80]:  # safety cap so a flood doesn't stall the dashboard
            cached_at = _aware(c.lead_score_updated_at) if c.lead_score_updated_at else None
            sig_at = latest_by_co.get(c.id)
            if cached_at is None or (sig_at and sig_at > cached_at):
                try:
                    await get_or_recompute(db, c, force=True)
                except Exception:
                    pass  # don't let one bad company break the dashboard

    # Now query the top hot leads by cached score
    hot_rows = (await db.execute(
        select(Company)
        .where(Company.lead_score >= 40)  # warm+; the UI labels by tier
        .order_by(Company.lead_score.desc())
        .limit(10)
    )).scalars().all()

    hot_leads = [
        {
            "id": c.id,
            "name": c.name,
            "status": c.status,
            "lead_score": c.lead_score,
            "lead_score_tier": c.lead_score_tier,
            "lead_score_fit": c.lead_score_fit,
            "lead_score_intent": c.lead_score_intent,
            # Back-compat field for any frontend code still reading it
            "engagement_score": c.lead_score,
            "signals": [],  # TODO: surface top components from lead_score_components
        }
        for c in hot_rows
    ]

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
            "days_stuck": (now - _aware(d.updated_at)).days if d.updated_at else 0,
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
            "reply_sentiment": a.reply_sentiment,
            "reply_sentiment_summary": a.reply_sentiment_summary,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a, cname, ufirst, ulast in feed_rows
    ]

    # MRR / ARR
    potential_mrr = pipeline_value  # monthly value of all open deals
    weighted_mrr = weighted_forecast
    potential_arr = potential_mrr * 12
    weighted_arr = weighted_mrr * 12
    won_mrr = won_mtd_value
    won_arr = won_mrr * 12

    return {
        "kpis": {
            "pipeline_value": round(pipeline_value, 2),
            "weighted_forecast": round(weighted_forecast, 2),
            "potential_mrr": round(potential_mrr, 2),
            "weighted_mrr": round(weighted_mrr, 2),
            "potential_arr": round(potential_arr, 2),
            "weighted_arr": round(weighted_arr, 2),
            "won_mtd_value": round(won_mtd_value, 2),
            "won_mtd_count": won_mtd_count,
            "won_mrr": round(won_mrr, 2),
            "won_arr": round(won_arr, 2),
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
    from app.scoping import scope_companies
    q = (
        select(Activity, Company.name, User.first_name, User.last_name)
        .join(Company, Activity.company_id == Company.id)
        .outerjoin(User, Activity.user_id == User.id)
        .order_by(Activity.created_at.desc())
        .limit(min(limit, 200))
    )
    q = scope_companies(q, user)
    rows = (await db.execute(q)).all()
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
    if user.role not in ("admin", "super_admin"):
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


# ============================================================
# BDR Activity Report — comprehensive view of what each rep is doing
# ============================================================

@router.get("/dashboard/bdr-activity")
async def bdr_activity_report(
    days: int = 7,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Admin-only comprehensive BDR activity report. Shows all actions per rep."""
    if user.role not in ("admin", "super_admin"):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Admin access required")

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 90)))

    # Get all activities in the window
    activities = (await db.execute(
        select(Activity).where(Activity.created_at >= cutoff)
    )).scalars().all()

    # Get all users
    users = (await db.execute(select(User).where(User.is_active == True))).scalars().all()
    user_map = {u.id: u for u in users}

    # Build per-rep stats
    reps = {}
    for u in users:
        if u.role in ("sales_rep", "admin", "super_admin"):
            reps[u.id] = {
                "user_id": u.id,
                "name": u.full_name,
                "role": u.role,
                "calls_made": 0,
                "calls_connected": 0,
                "emails_sent": 0,
                "sequences_started": 0,
                "notes_added": 0,
                "tasks_completed": 0,
                "companies_enriched": 0,
                "deals_created": 0,
                "deals_won": 0,
                "talk_minutes": 0,
                "last_activity": None,
            }

    for a in activities:
        if a.user_id not in reps:
            continue
        r = reps[a.user_id]

        if a.activity_type in ("call", "voicemail"):
            r["calls_made"] += 1
            if a.call_outcome == "connected":
                r["calls_connected"] += 1
            if a.call_duration_seconds:
                r["talk_minutes"] += a.call_duration_seconds / 60

        elif a.activity_type == "email_sent":
            r["emails_sent"] += 1

        elif a.activity_type == "sequence_created":
            r["sequences_started"] += 1

        elif a.activity_type in ("note", "meeting", "linkedin_message"):
            r["notes_added"] += 1

        elif a.activity_type == "task_completed":
            r["tasks_completed"] += 1

        elif a.activity_type == "enriched":
            r["companies_enriched"] += 1

        elif a.activity_type == "deal_created":
            r["deals_created"] += 1

        elif a.activity_type == "deal_update" and "closed_won" in (a.content or ""):
            r["deals_won"] += 1

        if not r["last_activity"] or (a.created_at and a.created_at.isoformat() > r["last_activity"]):
            r["last_activity"] = a.created_at.isoformat() if a.created_at else None

    # Round talk minutes
    for r in reps.values():
        r["talk_minutes"] = round(r["talk_minutes"], 1)
        r["connect_rate"] = round(r["calls_connected"] / max(r["calls_made"], 1) * 100, 1)

    # Sort by total activity
    sorted_reps = sorted(reps.values(), key=lambda r: -(
        r["calls_made"] + r["emails_sent"] + r["sequences_started"] + r["notes_added"]
    ))

    # Team totals
    totals = {
        "calls_made": sum(r["calls_made"] for r in reps.values()),
        "calls_connected": sum(r["calls_connected"] for r in reps.values()),
        "emails_sent": sum(r["emails_sent"] for r in reps.values()),
        "sequences_started": sum(r["sequences_started"] for r in reps.values()),
        "notes_added": sum(r["notes_added"] for r in reps.values()),
        "tasks_completed": sum(r["tasks_completed"] for r in reps.values()),
        "companies_enriched": sum(r["companies_enriched"] for r in reps.values()),
        "deals_created": sum(r["deals_created"] for r in reps.values()),
        "talk_minutes": round(sum(r["talk_minutes"] for r in reps.values()), 1),
    }

    return {
        "days": days,
        "totals": totals,
        "reps": sorted_reps,
    }


# ============================================================
# Recent Calls widget — for dashboard call review
# ============================================================

@router.get("/dashboard/recent-calls")
async def recent_calls(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Recent calls with recordings, transcripts, ratings. Scoped by role."""
    query = (
        select(Activity, Company.name.label("company_name"),
               User.first_name.label("rep_first"), User.last_name.label("rep_last"))
        .outerjoin(Company, Activity.company_id == Company.id)
        .outerjoin(User, Activity.user_id == User.id)
        .where(Activity.activity_type.in_(("call", "voicemail")))
        .order_by(Activity.created_at.desc())
        .limit(min(limit, 50))
    )

    # Scope: reps see only their calls
    if user.role not in ("admin", "super_admin"):
        query = query.where(Activity.user_id == user.id)

    rows = (await db.execute(query)).all()

    return [
        {
            "id": a.id,
            "company_id": a.company_id,
            "company_name": cname,
            "contact_id": a.contact_id,
            "rep_name": f"{rfirst or ''} {rlast or ''}".strip(),
            "user_id": a.user_id,
            "content": a.content,
            "call_direction": a.call_direction,
            "call_outcome": a.call_outcome,
            "call_duration_seconds": a.call_duration_seconds,
            "recording_url": bool(a.recording_url),
            "has_transcript": bool(a.transcript),
            "has_summary": bool(a.call_summary),
            "transcript": a.transcript,
            "call_summary": a.call_summary,
            "call_rating": a.call_rating,
            "call_feedback": a.call_feedback,
            "rated_by": a.rated_by,
            "rated_at": a.rated_at.isoformat() if a.rated_at else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a, cname, rfirst, rlast in rows
    ]


# ============================================================
# Rate a call — admin feedback on BDR performance
# ============================================================

from pydantic import BaseModel as _BaseModel

class RateCallRequest(_BaseModel):
    rating: int  # 1-5
    feedback: Optional[str] = None


@router.post("/dashboard/rate-call/{activity_id}")
async def rate_call(
    activity_id: int,
    req: RateCallRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Rate a call 1-5 stars with optional written feedback."""
    if user.role not in ("admin", "super_admin"):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Only admins can rate calls")

    if req.rating < 1 or req.rating > 5:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Rating must be 1-5")

    activity = (await db.execute(
        select(Activity).where(Activity.id == activity_id)
    )).scalar_one_or_none()

    if not activity:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Call not found")

    activity.call_rating = req.rating
    activity.call_feedback = req.feedback
    activity.rated_by = user.id
    activity.rated_at = datetime.now(timezone.utc)
    await db.commit()

    return {
        "id": activity.id,
        "call_rating": activity.call_rating,
        "call_feedback": activity.call_feedback,
        "rated_by": user.id,
    }
