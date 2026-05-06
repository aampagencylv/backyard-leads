"""
Public unsubscribe endpoint — no auth required, signed by per-contact token.
Marks the contact as unsubscribed and pauses any active sequence.
CAN-SPAM requires this to be honored within 10 business days; we do it instantly.
"""
from __future__ import annotations
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import Contact, GeneratedEmail, Activity

router = APIRouter(tags=["unsubscribe"])


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(
    t: str = Query(..., min_length=10, description="Per-contact unsubscribe token"),
    db: AsyncSession = Depends(get_db),
):
    contact = (await db.execute(
        select(Contact).where(Contact.unsubscribe_token == t)
    )).scalar_one_or_none()

    if not contact:
        return HTMLResponse(_page("Unsubscribe link is no longer valid", "If you continue to receive emails, reply to one and we'll remove you manually."), status_code=404)

    if not contact.unsubscribed_at:
        contact.unsubscribed_at = datetime.now(timezone.utc)
        # Pause any pending emails for this contact
        pending = (await db.execute(
            select(GeneratedEmail).where(
                GeneratedEmail.contact_id == contact.id,
                GeneratedEmail.is_sent == False,
                GeneratedEmail.paused_at.is_(None),
            )
        )).scalars().all()
        now = datetime.now(timezone.utc)
        for e in pending:
            e.paused_at = now
        db.add(Activity(
            company_id=contact.company_id,
            contact_id=contact.id,
            activity_type="unsubscribed",
            content=f"Unsubscribed via email link; {len(pending)} pending email(s) paused",
        ))
        await db.commit()

    return HTMLResponse(_page(
        "You've been unsubscribed",
        "We've removed you from this email list. You won't receive any further messages from Backyard Marketing Pros related to this outreach. Thanks for letting us know.",
    ))


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title} — Backyard Marketing Pros</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; background: #f7f9f7; margin: 0; padding: 40px 20px; color: #333; }}
.card {{ max-width: 520px; margin: 60px auto; background: white; border-radius: 12px; padding: 32px; box-shadow: 0 4px 20px rgba(0,0,0,0.06); }}
h1 {{ color: #1e4634; margin: 0 0 12px; font-size: 22px; }}
p {{ line-height: 1.6; font-size: 14px; }}
.brand {{ color: #FF723F; font-weight: 600; }}
</style>
</head>
<body>
  <div class="card">
    <h1>{title}</h1>
    <p>{body}</p>
    <p style="margin-top:24px;font-size:12px;color:#888">— <span class="brand">Backyard Marketing Pros</span></p>
  </div>
</body>
</html>"""
