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
from app.auth import get_current_user, mint_recording_token

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

STALE_DEAL_DAYS = 14


def _parse_or_none(raw):
    """Lazy JSON parse — returns None on empty/invalid so the client
    can just check truthiness."""
    if not raw:
        return None
    try:
        import json as _json
        return _json.loads(raw)
    except (ValueError, TypeError):
        return None


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
    rep_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Admin/super_admin can view any BDR's dashboard by passing rep_id.
    # When set, we impersonate that user for scoping purposes so the
    # dashboard shows THEIR data, not the admin's.
    effective_user = user
    if rep_id and user.role in ("admin", "super_admin") and rep_id != user.id:
        from app.models import User as _U
        target = (await db.execute(select(_U).where(_U.id == rep_id))).scalar_one_or_none()
        if target:
            effective_user = target
    # Use effective_user for all scoping below
    user = effective_user
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    cutoff_engagement = now - timedelta(days=ENGAGEMENT_LOOKBACK_DAYS)
    cutoff_stale = now - timedelta(days=STALE_DEAL_DAYS)

    # ---------- Pipeline-by-stage ----------
    from app.scoping import scope_deals, scope_companies
    from app.services import pipeline_config as _pc
    open_stage_keys = await _pc.get_open_stage_keys(db)
    dashboard_stage_keys = list(open_stage_keys) + ["closed_won", "closed_lost"]
    deal_query = scope_deals(select(Deal).where(Deal.pipeline == "default"), user)
    deals_open_or_done = (await db.execute(deal_query)).scalars().all()

    fallback_stage = open_stage_keys[0] if open_stage_keys else "in_sequence"
    by_stage: dict[str, dict] = {s: {"count": 0, "value": 0.0} for s in dashboard_stage_keys}
    pipeline_value = 0.0
    weighted_forecast = 0.0
    for d in deals_open_or_done:
        # If a deal is on a stage that's no longer in the config (e.g.
        # admin renamed/deleted it mid-flight), bucket it into the first
        # surviving open stage so totals don't lose it.
        stage = d.stage if d.stage in by_stage else fallback_stage
        if stage not in by_stage:
            continue
        v = d.value or 0
        by_stage[stage]["count"] += 1
        by_stage[stage]["value"] += v
        if stage in open_stage_keys:
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

    # Now query the top hot leads by cached score — scoped to this user's companies
    hot_q = (
        select(Company)
        .where(Company.lead_score >= 40)  # warm+; the UI labels by tier
        .order_by(Company.lead_score.desc())
        .limit(10)
    )
    hot_q = scope_companies(hot_q, user)
    hot_rows = (await db.execute(hot_q)).scalars().all()

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
    queued_q = (
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
    )
    queued_q = scope_companies(queued_q, user)
    queued_rows = (await db.execute(queued_q)).all()
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
    stuck_q = (
        select(Deal, Company.name)
        .join(Company, Deal.company_id == Company.id)
        .where(
            Deal.stage.in_(open_stage_keys),
            Deal.updated_at <= cutoff_stale,
        )
        .order_by(Deal.updated_at)
        .limit(10)
    )
    stuck_q = scope_deals(stuck_q, user)
    stuck_rows = (await db.execute(stuck_q)).all()
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
    sent_q = (
        select(func.count()).select_from(GeneratedEmail)
        .join(Company, GeneratedEmail.company_id == Company.id)
        .where(GeneratedEmail.is_sent == True, GeneratedEmail.sent_at >= week_start)
    )
    sent_q = scope_companies(sent_q, user)
    sent_this_week = (await db.execute(sent_q)).scalar() or 0

    # ---------- Activity feed (last 20, scoped to user's companies) ----------
    feed_q = (
        select(Activity, Company.name, User.first_name, User.last_name)
        .join(Company, Activity.company_id == Company.id)
        .outerjoin(User, Activity.user_id == User.id)
        .order_by(Activity.created_at.desc())
        .limit(20)
    )
    feed_q = scope_companies(feed_q, user)
    feed_rows = (await db.execute(feed_q)).all()
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
            for s in dashboard_stage_keys
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
            "has_recording": bool(a.recording_url),
            # Pre-signed streaming URL — the token is scoped to this
            # activity_id only, expires in 30 minutes. Lets <audio>/wavesurfer
            # play the file without needing to attach a bearer header.
            "recording_url": (
                f"/api/twilio/recording/{a.id}?t={mint_recording_token(a.id, user.id)}"
                if a.recording_url else None
            ),
            "has_transcript": bool(a.transcript),
            "has_summary": bool(a.call_summary),
            "transcript": a.transcript,
            "call_summary": a.call_summary,
            # Structured diarization for the dual-channel waveform.
            # Parsed client-side; null when transcription pre-dates the
            # persistence change (backfill will fill these in).
            "diarized_segments": _parse_or_none(a.diarized_segments_json),
            "talk_ratio": _parse_or_none(a.talk_ratio_json),
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


# ============================================================
# Team / Manager Dashboard
# ============================================================
#
# One big aggregation endpoint that powers the admin's "Team Overview"
# tab. All zones are computed server-side because:
#   1. Avoids 6+ round trips on first load
#   2. The activity-table scan only happens once
#   3. The client doesn't need to know our schema or denormalization
#
# Auth: admin / super_admin only. Returns everything role-scoped — a
# manager sees the whole org, no per-rep filter.

@router.get("/dashboard/team")
async def team_dashboard(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role not in ("admin", "super_admin"):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Admin access required")

    from app.auth import mint_recording_token

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())  # Mon = 0
    last_week_start = week_start - timedelta(days=7)
    last_week_end = week_start
    month_start = today_start.replace(day=1)
    sevenday_ago = now - timedelta(days=7)
    fourteenday_ago = now - timedelta(days=14)
    thirtyday_ago = now - timedelta(days=30)
    stale_cutoff = now - timedelta(days=STALE_DEAL_DAYS)

    # ----- Users in scope (active reps + admins) -----
    users = (await db.execute(
        select(User).where(User.is_active == True)
    )).scalars().all()
    bdr_ids = {u.id for u in users if u.role in ("sales_rep", "senior_rep", "admin", "super_admin")}
    user_map = {u.id: u for u in users}

    # ----- Pull every Activity in the last 30 days once -----
    activities_30d = (await db.execute(
        select(Activity).where(Activity.created_at >= thirtyday_ago)
    )).scalars().all()

    # ----- Pull every GeneratedEmail in the last 30 days too -----
    emails_30d = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.is_sent == True,
            GeneratedEmail.sent_at >= thirtyday_ago,
        )
    )).scalars().all()

    # ----- Bookings (meetings) in the last 30 days -----
    from app.models import Booking, Task as _Task
    bookings_30d = (await db.execute(
        select(Booking).where(Booking.created_at >= thirtyday_ago)
    )).scalars().all()

    # ----- Deals -----
    all_deals = (await db.execute(
        select(Deal).where(Deal.pipeline == "default")
    )).scalars().all()

    # ----- Tasks (open / overdue) -----
    open_tasks = (await db.execute(
        select(_Task).where(_Task.completed_at.is_(None))
    )).scalars().all()

    # ----- Company → owner prefetch -----
    # Previously each pass through activities/emails did N+1 lookups
    # against companies. With even a few hundred activities that becomes
    # noticeable. One single prefetch covers every company we'll see.
    needed_company_ids = set()
    for a in activities_30d:
        if a.company_id: needed_company_ids.add(a.company_id)
    for e in emails_30d:
        if e.company_id: needed_company_ids.add(e.company_id)
    for d in all_deals:
        if d.company_id: needed_company_ids.add(d.company_id)
    company_owner_cache: dict[int, int] = {}
    company_name_cache: dict[int, str] = {}
    if needed_company_ids:
        co_rows = (await db.execute(
            select(Company.id, Company.assigned_to, Company.name).where(Company.id.in_(needed_company_ids))
        )).all()
        for cid, owner, name in co_rows:
            company_owner_cache[cid] = owner or 0
            company_name_cache[cid] = name

    # ============================================================
    # Zone 1: Team KPI strip (with WoW deltas)
    # ============================================================

    def _count_acts(items, pred):
        return sum(1 for x in items if pred(x))

    calls_today = _count_acts(activities_30d, lambda a: a.activity_type == "call" and _aware(a.created_at) >= today_start)
    calls_this_week = _count_acts(activities_30d, lambda a: a.activity_type == "call" and _aware(a.created_at) >= week_start)
    calls_last_week = _count_acts(activities_30d, lambda a: a.activity_type == "call" and last_week_start <= _aware(a.created_at) < last_week_end)
    emails_today = _count_acts(emails_30d, lambda e: e.sent_at and _aware(e.sent_at) >= today_start)
    emails_this_week = _count_acts(emails_30d, lambda e: e.sent_at and _aware(e.sent_at) >= week_start)
    emails_last_week = _count_acts(emails_30d, lambda e: e.sent_at and last_week_start <= _aware(e.sent_at) < last_week_end)
    meetings_this_week = _count_acts(bookings_30d, lambda b: _aware(b.created_at) >= week_start and b.status == "confirmed")
    meetings_last_week = _count_acts(bookings_30d, lambda b: last_week_start <= _aware(b.created_at) < last_week_end and b.status == "confirmed")
    deals_won_mtd = sum((d.value or 0) for d in all_deals if d.stage == "closed_won" and d.closed_at and _aware(d.closed_at) >= month_start)
    deals_won_mtd_count = sum(1 for d in all_deals if d.stage == "closed_won" and d.closed_at and _aware(d.closed_at) >= month_start)

    def _wow(curr: int, prev: int) -> dict:
        if prev == 0:
            return {"pct": None, "direction": "flat" if curr == 0 else "up"}
        delta = (curr - prev) * 100 / prev
        return {
            "pct": round(delta, 0),
            "direction": "up" if delta > 1 else ("down" if delta < -1 else "flat"),
        }

    kpis = {
        "calls_today": calls_today,
        "calls_this_week": calls_this_week,
        "calls_wow": _wow(calls_this_week, calls_last_week),
        "emails_today": emails_today,
        "emails_this_week": emails_this_week,
        "emails_wow": _wow(emails_this_week, emails_last_week),
        "meetings_this_week": meetings_this_week,
        "meetings_wow": _wow(meetings_this_week, meetings_last_week),
        "deals_won_mtd_value": round(deals_won_mtd, 2),
        "deals_won_mtd_count": deals_won_mtd_count,
    }

    # ============================================================
    # Zone 2: BDR leaderboard (one row per active rep)
    # ============================================================

    per_bdr: dict[int, dict] = {
        uid: {
            "user_id": uid,
            "name": user_map[uid].full_name or user_map[uid].email,
            "email": user_map[uid].email,
            "role": user_map[uid].role,
            "calls_today": 0,
            "emails_today": 0,
            "imessages_today": 0,
            "calls_this_week": 0,
            "emails_this_week": 0,
            "meetings_this_week": 0,
            "open_pipeline_value": 0.0,
            "open_deal_count": 0,
            "stalled_deal_count": 0,
            "overdue_task_count": 0,
            "over_talked_calls_7d": 0,
            "last_activity_at": None,
        }
        for uid in bdr_ids
    }

    # Activities → calls_today / imessages / last_activity
    for a in activities_30d:
        if a.user_id not in per_bdr:
            continue
        row = per_bdr[a.user_id]
        created = _aware(a.created_at)
        # Track most-recent activity timestamp
        if row["last_activity_at"] is None or created > row["last_activity_at"]:
            row["last_activity_at"] = created
        if a.activity_type == "call":
            if created >= today_start: row["calls_today"] += 1
            if created >= week_start: row["calls_this_week"] += 1
            # Over-talked calls (7d) — skip single-speaker recordings
            if a.recording_url and a.talk_ratio_json and created >= sevenday_ago:
                try:
                    import json as _json
                    tr = _json.loads(a.talk_ratio_json)
                    if not tr.get("single_speaker") and (tr.get("rep_pct") or 0) > 60:
                        row["over_talked_calls_7d"] += 1
                except (ValueError, TypeError):
                    pass
        elif a.activity_type == "imessage_sent":
            if created >= today_start: row["imessages_today"] += 1

    # Emails → emails_today / emails_this_week (attribute to the
    # company's assigned rep via prefetched cache)
    for e in emails_30d:
        sent = _aware(e.sent_at) if e.sent_at else None
        if not sent:
            continue
        sender_uid = company_owner_cache.get(e.company_id) if e.company_id else None
        if not sender_uid or sender_uid not in per_bdr:
            continue
        if sent >= today_start: per_bdr[sender_uid]["emails_today"] += 1
        if sent >= week_start: per_bdr[sender_uid]["emails_this_week"] += 1

    # Bookings → meetings_this_week per host
    for b in bookings_30d:
        if b.host_user_id not in per_bdr:
            continue
        if _aware(b.created_at) >= week_start and b.status == "confirmed":
            per_bdr[b.host_user_id]["meetings_this_week"] += 1

    # Deal aggregates
    closed_stages = {"closed_won", "closed_lost", "snoozed"}
    for d in all_deals:
        if d.assigned_to not in per_bdr:
            continue
        if d.stage in closed_stages:
            continue
        per_bdr[d.assigned_to]["open_pipeline_value"] += (d.value or 0)
        per_bdr[d.assigned_to]["open_deal_count"] += 1
        if d.updated_at and _aware(d.updated_at) < stale_cutoff:
            per_bdr[d.assigned_to]["stalled_deal_count"] += 1

    # Tasks (overdue)
    for t in open_tasks:
        if t.user_id not in per_bdr:
            continue
        if t.due_date and _aware(t.due_date) < today_start:
            per_bdr[t.user_id]["overdue_task_count"] += 1

    # Sort: BDRs first by activity volume, admins go at the bottom
    def _sort_key(row):
        role_rank = {"sales_rep": 0, "senior_rep": 1, "admin": 2, "super_admin": 3}.get(row["role"], 4)
        return (role_rank, -(row["calls_this_week"] + row["emails_this_week"]))

    leaderboard = sorted(per_bdr.values(), key=_sort_key)
    for row in leaderboard:
        row["last_activity_at"] = row["last_activity_at"].isoformat() if row["last_activity_at"] else None
        row["open_pipeline_value"] = round(row["open_pipeline_value"], 2)

    # ============================================================
    # Zone 3: Coaching watchlist — over-talked + unrated calls (7d)
    # ============================================================

    watchlist: list[dict] = []
    for a in activities_30d:
        if a.activity_type != "call" or not a.recording_url:
            continue
        created = _aware(a.created_at)
        if created < sevenday_ago:
            continue
        # Parse talk_ratio if present — skip single-speaker recordings
        # for over-talking flag, but still surface for review
        rep_pct = None
        is_single_speaker = False
        if a.talk_ratio_json:
            try:
                import json as _json
                tr = _json.loads(a.talk_ratio_json)
                rep_pct = float(tr.get("rep_pct") or 0)
                is_single_speaker = bool(tr.get("single_speaker"))
            except (ValueError, TypeError):
                rep_pct = None
        over_talked = (not is_single_speaker) and rep_pct is not None and rep_pct > 60
        needs_review = a.call_rating is None
        if not (over_talked or needs_review):
            continue
        rep = user_map.get(a.user_id)
        co_name = company_name_cache.get(a.company_id) if a.company_id else None
        watchlist.append({
            "activity_id": a.id,
            "company_id": a.company_id,
            "company_name": co_name,
            "rep_name": rep.full_name if rep else None,
            "rep_id": a.user_id,
            "duration_seconds": a.call_duration_seconds,
            "rep_pct": rep_pct,
            "call_rating": a.call_rating,
            "over_talked": over_talked,
            "needs_review": needs_review,
            "recording_url": (
                f"/api/twilio/recording/{a.id}?t={mint_recording_token(a.id, user.id)}"
                if a.recording_url else None
            ),
            "created_at": created.isoformat(),
        })
    # Most-recent first; cap so we don't flood the page
    watchlist.sort(key=lambda x: x["created_at"], reverse=True)
    watchlist = watchlist[:20]

    # ============================================================
    # Zone 4: Stuck deals grouped by BDR (open, no movement in 14d)
    # ============================================================

    stuck_by_bdr: dict[int, list] = {uid: [] for uid in bdr_ids}
    for d in all_deals:
        if d.assigned_to not in stuck_by_bdr:
            continue
        if d.stage in closed_stages:
            continue
        if not d.updated_at or _aware(d.updated_at) >= stale_cutoff:
            continue
        stuck_by_bdr[d.assigned_to].append({
            "deal_id": d.id,
            "company_id": d.company_id,
            "company_name": company_name_cache.get(d.company_id),
            "stage": d.stage,
            "value": d.value or 0,
            "days_stale": (now - _aware(d.updated_at)).days,
            "updated_at": _aware(d.updated_at).isoformat(),
        })
    # Sort each rep's pile by days_stale desc, cap at 8 per rep
    stuck_groups = []
    for uid, items in stuck_by_bdr.items():
        if not items:
            continue
        items.sort(key=lambda x: -x["days_stale"])
        stuck_groups.append({
            "user_id": uid,
            "name": user_map[uid].full_name,
            "total_count": len(items),
            "deals": items[:8],
        })
    stuck_groups.sort(key=lambda g: -g["total_count"])

    # ============================================================
    # Zone 5: Reply sentiment breakdown per BDR (30d)
    # ============================================================
    #
    # email_replied activities don't carry user_id (the activity is
    # logged by the inbound webhook). Attribute to the company's
    # assigned rep instead.

    sentiment_buckets = ["interested", "objection", "out_of_office", "wrong_person", "unsubscribe", "other"]
    sentiment_by_bdr: dict[int, dict] = {
        uid: {s: 0 for s in sentiment_buckets} | {"total": 0, "name": user_map[uid].full_name}
        for uid in bdr_ids
    }
    for a in activities_30d:
        if a.activity_type != "email_replied":
            continue
        if not a.reply_sentiment:
            continue
        # Attribute via company owner (prefetched cache)
        if not a.company_id:
            continue
        uid = company_owner_cache.get(a.company_id)
        if not uid or uid not in sentiment_by_bdr:
            continue
        s = a.reply_sentiment.strip().lower()
        if s in sentiment_buckets:
            sentiment_by_bdr[uid][s] += 1
            sentiment_by_bdr[uid]["total"] += 1
        else:
            sentiment_by_bdr[uid]["other"] += 1
            sentiment_by_bdr[uid]["total"] += 1
    sentiment_rows = [v | {"user_id": uid} for uid, v in sentiment_by_bdr.items() if v["total"] > 0]
    sentiment_rows.sort(key=lambda x: -x["total"])

    # ============================================================
    # Zone 6: 14-day activity heatmap
    # ============================================================
    #
    # Rows = each BDR, cols = each day in the last 14. Values = count
    # of countable activities (calls + emails sent + imessages sent +
    # notes added). Frontend renders the matrix as colored cells.

    countable_types = {"call", "voicemail", "email_sent", "imessage_sent", "note"}
    days_axis = [(today_start - timedelta(days=i)).date().isoformat() for i in range(13, -1, -1)]
    heatmap_by_bdr: dict[int, dict[str, int]] = {uid: {d: 0 for d in days_axis} for uid in bdr_ids}
    for a in activities_30d:
        if a.user_id not in heatmap_by_bdr:
            continue
        if a.activity_type not in countable_types:
            continue
        d = _aware(a.created_at).date().isoformat()
        if d in heatmap_by_bdr[a.user_id]:
            heatmap_by_bdr[a.user_id][d] += 1
    # Also fold in sent emails (which live on GeneratedEmail, not Activity)
    for e in emails_30d:
        if not e.sent_at: continue
        sent = _aware(e.sent_at)
        if sent < fourteenday_ago: continue
        uid = company_owner_cache.get(e.company_id)
        if not uid or uid not in heatmap_by_bdr: continue
        d = sent.date().isoformat()
        if d in heatmap_by_bdr[uid]:
            heatmap_by_bdr[uid][d] += 1

    heatmap_rows = []
    max_cell = 0
    for uid, by_day in heatmap_by_bdr.items():
        cells = [by_day[d] for d in days_axis]
        cell_total = sum(cells)
        if cell_total == 0:
            continue
        max_cell = max(max_cell, max(cells))
        heatmap_rows.append({
            "user_id": uid,
            "name": user_map[uid].full_name,
            "total": cell_total,
            "cells": cells,
        })
    heatmap_rows.sort(key=lambda r: -r["total"])
    heatmap = {
        "days": days_axis,
        "rows": heatmap_rows,
        "max_cell": max_cell or 1,
    }

    # ============================================================
    # Zone 7: Conversion funnel per BDR (30d)
    # ============================================================

    funnel_by_bdr: dict[int, dict] = {
        uid: {
            "user_id": uid,
            "name": user_map[uid].full_name,
            "sequences_started": 0,
            "opens": 0,
            "replies": 0,
            "meetings": 0,
            "won": 0,
        }
        for uid in bdr_ids
    }
    for a in activities_30d:
        if a.activity_type == "sequence_created" and a.user_id in funnel_by_bdr:
            funnel_by_bdr[a.user_id]["sequences_started"] += 1
        elif a.activity_type == "email_opened" and a.company_id:
            uid = company_owner_cache.get(a.company_id)
            if uid and uid in funnel_by_bdr:
                funnel_by_bdr[uid]["opens"] += 1
        elif a.activity_type == "email_replied" and a.company_id:
            uid = company_owner_cache.get(a.company_id)
            if uid and uid in funnel_by_bdr:
                funnel_by_bdr[uid]["replies"] += 1
    for b in bookings_30d:
        if b.host_user_id in funnel_by_bdr and b.status == "confirmed":
            funnel_by_bdr[b.host_user_id]["meetings"] += 1
    for d in all_deals:
        if d.assigned_to in funnel_by_bdr and d.stage == "closed_won" and d.closed_at and _aware(d.closed_at) >= thirtyday_ago:
            funnel_by_bdr[d.assigned_to]["won"] += 1

    funnel_rows = [row for row in funnel_by_bdr.values() if row["sequences_started"] or row["opens"] or row["meetings"]]
    funnel_rows.sort(key=lambda r: -(r["meetings"] * 10 + r["won"] * 100 + r["sequences_started"]))

    # ============================================================
    # Zone 7: Full call log — every call with a recording, for grading
    # ============================================================

    all_calls: list[dict] = []
    for a in activities_30d:
        if a.activity_type != "call":
            continue
        created = _aware(a.created_at)
        rep = user_map.get(a.user_id)
        co_name = company_name_cache.get(a.company_id) if a.company_id else None

        # Parse talk ratio
        rep_pct = None
        prospect_pct = None
        is_single_speaker = False
        if a.talk_ratio_json:
            try:
                import json as _json
                tr = _json.loads(a.talk_ratio_json)
                rep_pct = float(tr.get("rep_pct") or 0)
                prospect_pct = float(tr.get("prospect_pct") or 0)
                is_single_speaker = bool(tr.get("single_speaker"))
            except (ValueError, TypeError):
                pass

        # Parse diarized segments for waveform rendering
        diarized = None
        if a.diarized_segments_json:
            try:
                diarized = json.loads(a.diarized_segments_json)
            except Exception:
                pass
        talk_ratio_parsed = None
        if a.talk_ratio_json:
            try:
                talk_ratio_parsed = json.loads(a.talk_ratio_json)
            except Exception:
                pass

        all_calls.append({
            "id": a.id,
            "activity_id": a.id,
            "company_id": a.company_id,
            "company_name": co_name,
            "contact_id": a.contact_id,
            "content": a.content,
            "rep_name": rep.full_name if rep else None,
            "rep_id": a.user_id,
            "call_duration_seconds": a.call_duration_seconds,
            "call_outcome": a.call_outcome,
            "call_direction": a.call_direction,
            "has_recording": bool(a.recording_url),
            "has_transcript": bool(a.transcript),
            "has_summary": bool(a.call_summary),
            "call_summary": a.call_summary,
            "call_rating": a.call_rating,
            "call_feedback": a.call_feedback,
            "diarized_segments": diarized,
            "talk_ratio": talk_ratio_parsed,
            "rep_pct": rep_pct,
            "prospect_pct": prospect_pct,
            "single_speaker": is_single_speaker,
            "rated_by": a.rated_by,
            "recording_url": (
                f"/api/twilio/recording/{a.id}?t={mint_recording_token(a.id, user.id)}"
                if a.recording_url else None
            ),
            "created_at": created.isoformat(),
        })
    all_calls.sort(key=lambda x: x["created_at"], reverse=True)

    # ============================================================
    # Zone 8: Hot leads by BDR
    # ============================================================

    hot_by_bdr: list[dict] = []
    hot_companies = (await db.execute(
        select(Company)
        .where(Company.lead_score >= 40, Company.assigned_to.isnot(None))
        .order_by(Company.lead_score.desc())
        .limit(30)
    )).scalars().all()
    for c in hot_companies:
        rep = user_map.get(c.assigned_to)
        hot_by_bdr.append({
            "id": c.id,
            "name": c.name,
            "status": c.status,
            "lead_score": c.lead_score,
            "lead_score_tier": c.lead_score_tier,
            "rep_name": rep.full_name if rep else "Unassigned",
            "rep_id": c.assigned_to,
        })

    return {
        "generated_at": now.isoformat(),
        "window_days": 30,
        "kpis": kpis,
        "leaderboard": leaderboard,
        "coaching_watchlist": watchlist,
        "stuck_deals_by_bdr": stuck_groups,
        "reply_sentiment_by_bdr": sentiment_rows,
        "activity_heatmap": heatmap,
        "conversion_funnel": funnel_rows,
        "call_log": all_calls,
        "hot_leads_by_bdr": hot_by_bdr,
    }
