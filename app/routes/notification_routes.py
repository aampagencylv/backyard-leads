"""
Realtime-ish notifications via long polling.

Frontend polls /api/notifications/recent?since=<iso> every 30 seconds (60s when
the tab is hidden) and surfaces any "notable" activities created since the last
poll as toasts + native browser notifications.

Notable activity types — the ones a BDR genuinely wants to be interrupted for:
  - hot_lead          — 🔥 prospect actively on the website (page threshold or high-intent action)
  - imessage_received — text reply from a contact
  - email_replied     — email reply

Deliberately excluded (too noisy):
  - email_opened, email_clicked  — useful in the timeline, not as interruptions
  - sequence_step_skipped         — informational only
  - task_created                  — surfaces in the tasks page
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import User, Activity, Contact, Company
from app.auth import get_current_user


router = APIRouter(prefix="/api/notifications", tags=["notifications"])


# Activity types that should pop up an interruption-style notification
NOTABLE_TYPES = ["hot_lead", "imessage_received", "email_replied"]


@router.get("/recent")
async def recent(
    since: Optional[str] = Query(None, description="ISO 8601 timestamp; only return activities created strictly after this"),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return notable activities since `since`. Cap at last 5 minutes if `since`
    is missing or earlier — avoids dumping hours of backlog when a user opens the
    app for the first time."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    if since:
        try:
            parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            cutoff = max(cutoff, parsed)
        except ValueError:
            pass  # malformed → fall back to 5-minute lookback

    # Multi-tenant scoping: a sales_rep should only get popups for THEIR
    # companies. Admins + super_admins still see everything (pre-existing
    # convention from app/scoping.py — keeps cross-team visibility for
    # supervisors). We join Company on the activity to filter by ownership
    # rather than reusing scope_companies because Activity ↔ Company is via
    # company_id only, not a relationship that scope_companies expects.
    q = (
        select(Activity)
        .where(
            Activity.activity_type.in_(NOTABLE_TYPES),
            Activity.created_at > cutoff,
        )
        .order_by(Activity.created_at.desc())
        .limit(limit)
    )
    if user.role not in ("admin", "super_admin"):
        q = q.join(Company, Activity.company_id == Company.id).where(Company.assigned_to == user.id)
    rows = (await db.execute(q)).scalars().all()

    # Pull contact + company names in one batch each
    contact_ids = {a.contact_id for a in rows if a.contact_id}
    company_ids = {a.company_id for a in rows if a.company_id}
    contacts = {c.id: c for c in (await db.execute(select(Contact).where(Contact.id.in_(contact_ids)))).scalars().all()} if contact_ids else {}
    companies = {co.id: co for co in (await db.execute(select(Company).where(Company.id.in_(company_ids)))).scalars().all()} if company_ids else {}

    server_now = datetime.now(timezone.utc)
    items = []
    for a in rows:
        c = contacts.get(a.contact_id) if a.contact_id else None
        co = companies.get(a.company_id) if a.company_id else None
        items.append({
            "id": a.id,
            "type": a.activity_type,
            "content": a.content,
            "company_id": a.company_id,
            "company_name": co.name if co else None,
            "contact_id": a.contact_id,
            "contact_name": c.full_name if c else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })

    # Return server_now so the client can use the SERVER's clock as `since` on the
    # next poll instead of trusting its own clock (avoids missing notifications
    # if the client's clock is skewed).
    return {"server_now": server_now.isoformat(), "items": items}
