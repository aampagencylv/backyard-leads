"""Email deliverability / reputation dashboard.

Read-only aggregation over the GeneratedEmail event timestamps that the
Resend webhook fills in. Surfaces the same numbers the Resend dashboard
shows, but per-recipient-domain and tied to our own contacts — so we
can see at a glance whether Gmail is treating us differently from
Outlook, and which contacts triggered recent bounces/complaints.

Auth: admin + super_admin. Sales reps don't need this view.

Thresholds we surface (per Google / industry consensus):
  - Bounce rate    > 5.0%   → ERROR (will land in spam folder)
                   > 2.0%   → WARN
  - Complaint rate > 0.3%   → ERROR (Google may suspend sending)
                   > 0.1%   → WARN
  - Open rate      < 10.0%  → WARN  (subject lines + warmup issue)
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select, func, and_, or_, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_super_admin, get_current_user
from app.database import get_db
from app.models import GeneratedEmail, Contact, User
from pydantic import BaseModel

router = APIRouter(prefix="/api/admin/reputation", tags=["admin-reputation"])
log = logging.getLogger("bmp.reputation")


class SpamCheckRequest(BaseModel):
    subject: str
    html_body: Optional[str] = None
    plain_body: Optional[str] = None


@router.post("/spam-check")
async def spam_check(
    req: SpamCheckRequest,
    _user: User = Depends(get_current_user),  # any authenticated user — editors call this from the email composer
) -> dict:
    """Score the provided email content against the local heuristic.
    Returns the same shape as score_email — caller renders it inline
    in the editor. Auth: any signed-in user (this is a non-mutating
    check used in the composer preview)."""
    from app.services.spam_score import score_email
    return score_email(
        subject=req.subject,
        html_body=req.html_body,
        plain_body=req.plain_body,
    )


def _classify_bounce(rate_pct: float) -> str:
    if rate_pct >= 5.0: return "error"
    if rate_pct >= 2.0: return "warn"
    return "ok"


def _classify_complaint(rate_pct: float) -> str:
    if rate_pct >= 0.3: return "error"
    if rate_pct >= 0.1: return "warn"
    return "ok"


def _classify_open(rate_pct: float) -> str:
    if rate_pct < 10.0: return "warn"
    return "ok"


def _safe_pct(numer: int, denom: int) -> float:
    if not denom:
        return 0.0
    return round(numer * 100.0 / denom, 2)


def _extract_domain(email: Optional[str]) -> str:
    if not email or "@" not in email:
        return "(unknown)"
    return email.split("@", 1)[1].strip().lower() or "(unknown)"


@router.get("/summary")
async def reputation_summary(
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_super_admin),
) -> dict:
    """Overall numbers + per-domain breakdown over the last `days` days.

    Counting policy: we count an email in a denominator the moment the
    Resend webhook tells us it left our outbox (delivered_at IS NOT NULL).
    That's stricter than is_sent (which can be set when our API call
    succeeded, before Resend actually delivered). Bounces / complaints
    are counted whenever the timestamp is set, regardless of delivery —
    a bounce IS a non-delivery, so it always counts.
    """
    days = max(1, min(days, 365))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Pull every email row in the window joined to its contact's email,
    # then aggregate in Python. SQLite-friendly and tiny at our scale —
    # if this becomes slow we move to GROUP BY in SQL.
    rows = (await db.execute(
        select(
            GeneratedEmail.id,
            GeneratedEmail.delivered_at,
            GeneratedEmail.opened_at,
            GeneratedEmail.open_count,
            GeneratedEmail.bounced_at,
            GeneratedEmail.complained_at,
            GeneratedEmail.sent_at,
            Contact.email,
        )
        .join(Contact, Contact.id == GeneratedEmail.contact_id)
        .where(
            or_(
                GeneratedEmail.delivered_at >= cutoff,
                GeneratedEmail.bounced_at >= cutoff,
                GeneratedEmail.sent_at >= cutoff,
            )
        )
    )).all()

    overall = {"sent": 0, "delivered": 0, "opened": 0, "bounced": 0, "complained": 0}
    by_domain: dict[str, dict] = {}
    daily: dict[str, dict] = {}  # iso-date → {sent, delivered, opened, bounced, complained}

    for r in rows:
        dom = _extract_domain(r.email)
        d = by_domain.setdefault(dom, {"sent": 0, "delivered": 0, "opened": 0, "bounced": 0, "complained": 0})
        # Bucket: prefer delivered_at, fall back to sent_at, fall back to bounced_at
        bucket_dt = r.delivered_at or r.sent_at or r.bounced_at
        bucket = bucket_dt.date().isoformat() if bucket_dt else None
        if bucket:
            db_day = daily.setdefault(bucket, {"sent": 0, "delivered": 0, "opened": 0, "bounced": 0, "complained": 0})
        else:
            db_day = None

        # sent denominator = we attempted to send (Resend POST succeeded)
        if r.sent_at:
            overall["sent"] += 1
            d["sent"] += 1
            if db_day: db_day["sent"] += 1
        if r.delivered_at:
            overall["delivered"] += 1
            d["delivered"] += 1
            if db_day: db_day["delivered"] += 1
        if r.opened_at:
            overall["opened"] += 1
            d["opened"] += 1
            if db_day: db_day["opened"] += 1
        if r.bounced_at:
            overall["bounced"] += 1
            d["bounced"] += 1
            if db_day: db_day["bounced"] += 1
        if r.complained_at:
            overall["complained"] += 1
            d["complained"] += 1
            if db_day: db_day["complained"] += 1

    # Compute rates + status on overall
    bounce_pct = _safe_pct(overall["bounced"], overall["sent"])
    complaint_pct = _safe_pct(overall["complained"], overall["delivered"])
    open_pct = _safe_pct(overall["opened"], overall["delivered"])
    delivery_pct = _safe_pct(overall["delivered"], overall["sent"])

    overall_status = "ok"
    overall_status_reasons: list[str] = []
    bc = _classify_bounce(bounce_pct)
    cc = _classify_complaint(complaint_pct)
    oc = _classify_open(open_pct) if overall["delivered"] >= 50 else "ok"  # need volume before warning
    for code, label in [(bc, f"bounce rate {bounce_pct}%"), (cc, f"complaint rate {complaint_pct}%"), (oc, f"open rate {open_pct}%")]:
        if code != "ok":
            overall_status_reasons.append(label)
        if {"ok": 0, "warn": 1, "error": 2}[code] > {"ok": 0, "warn": 1, "error": 2}[overall_status]:
            overall_status = code

    # Per-domain breakdown (sorted by sent volume desc, cap at 12 domains)
    domain_rows = []
    for dom, d in sorted(by_domain.items(), key=lambda kv: -kv[1]["sent"])[:12]:
        bp = _safe_pct(d["bounced"], d["sent"])
        cp = _safe_pct(d["complained"], d["delivered"])
        op = _safe_pct(d["opened"], d["delivered"])
        dp = _safe_pct(d["delivered"], d["sent"])
        domain_rows.append({
            "domain": dom,
            "sent": d["sent"],
            "delivered": d["delivered"],
            "opened": d["opened"],
            "bounced": d["bounced"],
            "complained": d["complained"],
            "delivery_pct": dp,
            "open_pct": op,
            "bounce_pct": bp,
            "complaint_pct": cp,
        })

    # Daily trend (last `days` days, in ascending order). Fill gaps with zeros
    # so the chart doesn't skip days where nothing happened.
    trend = []
    today = datetime.now(timezone.utc).date()
    for i in range(days - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        d = daily.get(day) or {"sent": 0, "delivered": 0, "opened": 0, "bounced": 0, "complained": 0}
        trend.append({"date": day, **d})

    # Recent offenders — last bounces + complaints
    recent_offenders_rows = (await db.execute(
        select(
            GeneratedEmail.id,
            GeneratedEmail.bounced_at,
            GeneratedEmail.complained_at,
            Contact.email,
            Contact.id.label("contact_id"),
            GeneratedEmail.company_id,
        )
        .join(Contact, Contact.id == GeneratedEmail.contact_id)
        .where(or_(GeneratedEmail.bounced_at >= cutoff, GeneratedEmail.complained_at >= cutoff))
        .order_by(func.coalesce(GeneratedEmail.complained_at, GeneratedEmail.bounced_at).desc())
        .limit(25)
    )).all()
    offenders = [{
        "email_id": r.id,
        "contact_id": r.contact_id,
        "company_id": r.company_id,
        "email": r.email,
        "kind": "complained" if r.complained_at else "bounced",
        "at": (r.complained_at or r.bounced_at).isoformat() if (r.complained_at or r.bounced_at) else None,
    } for r in recent_offenders_rows]

    return {
        "window_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall": {
            "sent": overall["sent"],
            "delivered": overall["delivered"],
            "opened": overall["opened"],
            "bounced": overall["bounced"],
            "complained": overall["complained"],
            "delivery_pct": delivery_pct,
            "open_pct": open_pct,
            "bounce_pct": bounce_pct,
            "complaint_pct": complaint_pct,
            "status": overall_status,
            "status_reasons": overall_status_reasons,
        },
        "thresholds": {
            "bounce_warn_pct":    2.0,
            "bounce_error_pct":   5.0,
            "complaint_warn_pct": 0.1,
            "complaint_error_pct":0.3,
            "open_warn_pct":      10.0,
            "min_volume_for_open_warn": 50,
        },
        "by_domain": domain_rows,
        "trend": trend,
        "recent_offenders": offenders,
    }
