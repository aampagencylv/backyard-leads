"""
Lead scoring v2 — fit × intent.

Replaces the legacy "3+ opens OR any click" Hot Leads heuristic with a
real per-company score driven by:

  FIT (firmographic match to ICP):
    - has website, has primary contact email, has decision-maker
    - rating + review count (sweet spot 20-300 reviews, 4.0+ stars)
    - business_type matches priority verticals
    - mobile-validated phone (Twilio Lookup line_type)
    - has LinkedIn URL on primary contact

  INTENT (recent engagement signals, exponentially decayed by age):
    - email opens, clicks, replies (with sentiment-weighted scoring)
    - 'interested' replies score MUCH higher than 'objection' or 'OOO'
    - hot lead activity, page views, form submits, tel-clicks
    - meeting_booked is the ceiling signal

Combined score = (fit + intent) / 2, capped at 100.
Tiers: burning ≥80, hot ≥60, warm ≥40, cool ≥20, cold otherwise.

Cost: zero — pure SQL-fed computation. Recomputed lazily when stale.
"""
from __future__ import annotations
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Company, Contact, Activity


# Recompute when cached value is older than this. 1h is a sweet spot —
# fresh enough to feel live for engagement signals, infrequent enough
# that hitting the dashboard for the 50th time today doesn't hammer the DB.
STALE_AFTER = timedelta(hours=1)


# Verticals BMP serves directly — bonus when business_type matches
PRIORITY_VERTICALS = (
    "pool", "landscap", "deck", "outdoor kitchen", "patio", "hardscape",
    "outdoor lighting", "fence", "pergola", "artificial turf", "concrete",
    "irrigation", "lawn", "backyard",
)


@dataclass
class ScoreResult:
    fit: int = 0
    intent: int = 0
    combined: int = 0
    tier: str = "cold"
    components: dict = field(default_factory=dict)
    last_signal_at: Optional[datetime] = None


# ============================================================
# Fit
# ============================================================

def _fit_score(company: Company, primary_contact: Optional[Contact]) -> tuple[int, dict]:
    score = 0
    out: dict = {}

    if company.website:
        score += 15
        out["has_website"] = 15

    if company.rating:
        if company.rating >= 4.5:
            score += 15
            out["rating_4_5_plus"] = 15
        elif company.rating >= 4.0:
            score += 10
            out["rating_4_plus"] = 10
        elif company.rating >= 3.5:
            score += 5
            out["rating_3_5_plus"] = 5

    if company.review_count:
        if 20 <= company.review_count <= 300:
            score += 15
            out["reviews_sweet_spot"] = 15
        elif company.review_count >= 10:
            score += 8
            out["reviews_some"] = 8
        elif company.review_count >= 3:
            score += 3
            out["reviews_few"] = 3

    bt = (company.business_type or "").lower()
    if bt and any(v in bt for v in PRIORITY_VERTICALS):
        score += 15
        out["priority_vertical"] = 15

    if primary_contact:
        if primary_contact.email:
            score += 12
            out["has_email"] = 12
            if (primary_contact.email_status or "").lower() in ("valid", "verified", "deliverable"):
                score += 5
                out["email_verified"] = 5
        if primary_contact.first_name and primary_contact.last_name:
            score += 8
            out["has_full_name"] = 8
        if primary_contact.phone_type == "mobile":
            score += 8
            out["phone_mobile"] = 8
        if primary_contact.linkedin_url:
            score += 5
            out["has_linkedin"] = 5

    return min(100, score), out


# ============================================================
# Intent
# ============================================================

# Per-event base weights — multiplied by exp(-days_since/14) before adding.
# Caps prevent any single signal type from dominating the score.
EVENT_WEIGHTS = {
    "email_opened":    (8,  40),   # +8 each, max +40 (5 effective opens)
    "email_clicked":   (25, 60),   # +25 each, max +60
    "hot_lead":        (60, 60),   # one signal already aggregates engagement
    "form_submit":     (70, 70),   # high-intent action
    "tel_click":       (50, 50),   # mobile call intent
    "mailto_click":    (30, 30),
    "outbound_click":  (20, 40),
    "pageview":        (5,  30),
    "meeting_booked":  (90, 90),   # near-ceiling — they ARE a hot lead
}

REPLY_SENTIMENT_WEIGHTS = {
    "interested":     80,
    "objection":      30,
    "out_of_office":   5,
    "wrong_person":    0,
    "unsubscribe":     0,
    "other":          15,
    None:             20,  # not yet classified — give some weight
}


# Engagement-engine signal codes → (base_weight, cap). Mirrors EVENT_WEIGHTS
# above so intent scoring is consistent regardless of which write-path
# delivered the data (Resend webhook → Activity rows, OR new dispatcher →
# signals rows, OR both during the cutover-back-compat period).
SIGNAL_WEIGHTS = {
    "email_open":         (8,  40),
    "email_click":        (25, 60),
    "email_reply":        (40, 80),  # sentiment-adjusted below
    "email_bounce":       (-30, -30),  # negative — bad email kills intent
    "email_complaint":    (-60, -60),
    "email_unsubscribe":  (-80, -80),
    "sms_reply":          (40, 80),
    "sms_opt_out":        (-80, -80),
    "call_outcome":       (50, 50),
    "meeting_booked":     (90, 90),
    "linkedin_profile_change": (15, 30),
    "linkedin_post":          (10, 25),
    "gmb_review":             (15, 30),
    "hiring_signal":          (12, 25),
    "press_mention":          (10, 20),
    "manual_note":            (5,  20),
}


# Activity.activity_type → canonical signal code. When BOTH an Activity row
# AND a Signal row carry the same event (Resend webhook dual-write during the
# back-compat window), we count it ONCE. The match is by canonical code +
# ±5-second observed_at bucket.
ACTIVITY_TO_SIGNAL_CODE = {
    "email_opened":   "email_open",
    "email_clicked":  "email_click",
    "email_replied":  "email_reply",
    "email_bounced":  "email_bounce",
    "email_complained": "email_complaint",
    "email_unsubscribed": "email_unsubscribe",
}


def _intent_score(
    activities: list[Activity],
    signals: Optional[list[dict]] = None,
) -> tuple[int, dict, Optional[datetime]]:
    """Compute the intent half of the lead score.

    Sources (both consumed, deduped by event-type within ±5s):
      - Activities: legacy Resend-webhook + manual UI writes (back-compat)
      - Signals:    new engagement-engine writes (richer — has engagement
                    + action linkage). When the same event lands in both
                    tables (because Resend webhook fires the dual-write),
                    only the signal counts.

    Bounce / complaint / opt-out signals carry NEGATIVE weight that can
    drag intent below zero — clamped to 0 at the bottom so the tier
    machinery doesn't see weird values.
    """
    now = datetime.now(timezone.utc)
    raw_total = 0.0
    out: dict = {}
    capped: dict[str, float] = {}
    last_signal_at: Optional[datetime] = None

    def _decay(days: float) -> float:
        return math.exp(-max(days, 0) / 14)  # ~10-day half-life

    def _bucket(ts: datetime) -> int:
        """5-second bucket from epoch for dedup key."""
        return int(ts.timestamp()) // 5

    # First pass: index signal (code, bucket) keys so activities can dedup.
    # We iterate signals FIRST below so the bucket set is populated when we
    # consult it from the activity loop.
    signal_buckets: set[tuple[str, int]] = set()
    for sig in (signals or []):
        observed = sig.get("observed_at")
        if not observed:
            continue
        ts = observed if observed.tzinfo else observed.replace(tzinfo=timezone.utc)
        code = sig.get("code") or ""
        if code:
            signal_buckets.add((code, _bucket(ts)))

    for a in activities:
        if not a.created_at:
            continue
        ts = a.created_at if a.created_at.tzinfo else a.created_at.replace(tzinfo=timezone.utc)
        days = (now - ts).total_seconds() / 86400
        if days < 0:
            days = 0
        if days > 60:
            continue

        kind = a.activity_type

        # Dedup: if there's a matching signal within ±5s, skip the activity
        # (the signal will be counted in the next loop with richer data).
        sig_equivalent = ACTIVITY_TO_SIGNAL_CODE.get(kind)
        if sig_equivalent:
            act_bucket = _bucket(ts)
            if (
                (sig_equivalent, act_bucket) in signal_buckets
                or (sig_equivalent, act_bucket - 1) in signal_buckets
                or (sig_equivalent, act_bucket + 1) in signal_buckets
            ):
                continue

        decay = _decay(days)
        weight = 0.0

        if kind == "email_replied":
            sent = (a.reply_sentiment or "").lower() or None
            base = REPLY_SENTIMENT_WEIGHTS.get(sent, 20)
            weight = base * decay
            label = f"reply_{sent or 'unclassified'}"
            capped[label] = capped.get(label, 0) + weight
            if last_signal_at is None or ts > last_signal_at:
                last_signal_at = ts
            continue
        elif kind in EVENT_WEIGHTS:
            base, cap = EVENT_WEIGHTS[kind]
            weight = base * decay
            running = capped.get(kind, 0) + weight
            capped[kind] = min(running, cap)
            if last_signal_at is None or ts > last_signal_at:
                last_signal_at = ts
            continue

        raw_total += weight
        if last_signal_at is None or ts > last_signal_at:
            last_signal_at = ts

    # Engagement-engine signals — added on top of activity-derived weights.
    # Each signal dict is {'code': str, 'observed_at': datetime,
    # 'reply_sentiment': str|None} as built by `_load_signals_for_company`.
    for sig in (signals or []):
        observed = sig.get("observed_at")
        if not observed:
            continue
        ts = observed if observed.tzinfo else observed.replace(tzinfo=timezone.utc)
        days = (now - ts).total_seconds() / 86400
        if days < 0:
            days = 0
        if days > 60:
            continue

        decay = _decay(days)
        code = sig.get("code") or ""

        if code == "email_reply":
            sent = (sig.get("reply_sentiment") or "").lower() or None
            base = REPLY_SENTIMENT_WEIGHTS.get(sent, 20)
            weight = base * decay
            label = f"reply_signal_{sent or 'unclassified'}"
            capped[label] = capped.get(label, 0) + weight
        elif code in SIGNAL_WEIGHTS:
            base, cap = SIGNAL_WEIGHTS[code]
            weight = base * decay
            running = capped.get(f"sig_{code}", 0) + weight
            # For negative weights, cap is the FLOOR (most negative)
            if base < 0:
                capped[f"sig_{code}"] = max(running, cap)
            else:
                capped[f"sig_{code}"] = min(running, cap)
        else:
            # Unknown signal type — small generic credit so unrecognized
            # data still counts as engagement.
            capped[f"sig_{code or 'unknown'}"] = capped.get(
                f"sig_{code or 'unknown'}", 0
            ) + 5 * decay

        if last_signal_at is None or ts > last_signal_at:
            last_signal_at = ts

    for k, v in capped.items():
        raw_total += v
        if v != 0:
            out[k] = round(v)

    score = max(0, min(100, round(raw_total)))
    return score, out, last_signal_at


async def _load_signals_for_company(
    db: AsyncSession, company_id: int,
) -> list[dict]:
    """Pull the last-60-days of engagement-engine signals for any contact
    at the company. Returns a list of small dicts keyed for the intent
    layer. Empty list on any error (so the legacy activity-only path
    still works during the cutover-back-compat window)."""
    try:
        rows = (await db.execute(text("""
            SELECT st.code, s.observed_at, s.raw_data_json
            FROM signals s
            JOIN signal_types st ON st.id = s.signal_type_id
            JOIN contacts c ON c.id = s.contact_id
            WHERE c.company_id = :co
              AND s.observed_at >= NOW() - INTERVAL '60 days'
            ORDER BY s.observed_at DESC
            LIMIT 500
        """), {"co": company_id})).fetchall()
    except Exception:
        return []

    out: list[dict] = []
    for r in rows:
        raw = r.raw_data_json or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        out.append({
            "code": r.code,
            "observed_at": r.observed_at,
            "reply_sentiment": raw.get("sentiment") or raw.get("reply_sentiment"),
        })
    return out


# ============================================================
# Combine + tier
# ============================================================

def _tier(combined: int) -> str:
    if combined >= 80: return "burning"
    if combined >= 60: return "hot"
    if combined >= 40: return "warm"
    if combined >= 20: return "cool"
    return "cold"


def _combine(fit: int, intent: int) -> int:
    """50/50 blend. Slight upward adjustment when both axes are non-zero
    to reward leads that match ICP AND are actively engaging — those
    are the ones to call today."""
    base = (fit + intent) / 2
    if fit >= 30 and intent >= 30:
        base *= 1.1
    return min(100, round(base))


def compute_score(
    company: Company,
    contacts: list[Contact],
    activities: list[Activity],
    signals: Optional[list[dict]] = None,
) -> ScoreResult:
    primary = next((c for c in contacts if c.is_primary), contacts[0] if contacts else None)
    fit, fit_components = _fit_score(company, primary)
    intent, intent_components, last_signal = _intent_score(activities, signals)
    combined = _combine(fit, intent)

    return ScoreResult(
        fit=fit,
        intent=intent,
        combined=combined,
        tier=_tier(combined),
        components={**fit_components, **intent_components},
        last_signal_at=last_signal,
    )


# ============================================================
# Persistence — lazy recompute
# ============================================================

async def get_or_recompute(db: AsyncSession, company: Company, *, force: bool = False) -> ScoreResult:
    """Return the company's lead score, recomputing if the cache is stale.

    Mutates company.lead_score* fields in-place + commits when a recompute
    actually runs. Safe to call from any read path.
    """
    fresh = (
        not force
        and company.lead_score_updated_at is not None
        and (datetime.now(timezone.utc) - (
            company.lead_score_updated_at if company.lead_score_updated_at.tzinfo
            else company.lead_score_updated_at.replace(tzinfo=timezone.utc)
        )) < STALE_AFTER
    )
    if fresh:
        return ScoreResult(
            fit=company.lead_score_fit or 0,
            intent=company.lead_score_intent or 0,
            combined=company.lead_score or 0,
            tier=company.lead_score_tier or "cold",
            components=json.loads(company.lead_score_components or "{}"),
            last_signal_at=None,
        )

    # Recompute. Pull contacts + activities for this company.
    contacts = (await db.execute(
        select(Contact).where(Contact.company_id == company.id)
        .order_by(Contact.is_primary.desc(), Contact.id)
    )).scalars().all()

    activities = (await db.execute(
        select(Activity).where(Activity.company_id == company.id)
        .order_by(Activity.created_at.desc())
        .limit(200)
    )).scalars().all()

    signals = await _load_signals_for_company(db, company.id)

    result = compute_score(company, list(contacts), list(activities), signals)

    company.lead_score = result.combined
    company.lead_score_fit = result.fit
    company.lead_score_intent = result.intent
    company.lead_score_tier = result.tier
    company.lead_score_components = json.dumps(result.components)
    company.lead_score_updated_at = datetime.now(timezone.utc)
    await db.commit()

    return result
