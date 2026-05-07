"""
Public click-tracking endpoint — handles /t/{token} from outgoing emails.

Drops the bmp_visitor cookie (Phase 2 will use it to attribute downstream
page views back to this contact), logs an Activity to the timeline, then
302s to the destination URL.

Public route — no auth. Performance-sensitive: every email click hits this.
Keep it under ~30ms by avoiding any unnecessary DB joins.
"""
from __future__ import annotations
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from app.database import async_session
from app.models import TrackingLink, Activity, Contact, Company


router = APIRouter(tags=["tracking"])


@router.get("/t/{token}")
async def track_click(token: str, request: Request):
    """Log the click + drop the visitor cookie + redirect to destination.

    Bots hit these too (link-checkers, mail-prefetchers like Apple's
    privacy-protection redirect, Outlook SafeLinks). We log every hit but
    only count the FIRST as 'first_clicked_at' on the link, so the timeline
    gets one canonical click event per recipient regardless of bot noise."""
    async with async_session() as db:
        link = (await db.execute(
            select(TrackingLink).where(TrackingLink.token == token)
        )).scalar_one_or_none()

        if not link:
            return Response(status_code=404, content="link not found")

        now = datetime.now(timezone.utc)
        is_first_click = link.first_clicked_at is None
        link.click_count = (link.click_count or 0) + 1
        link.last_clicked_at = now
        if is_first_click:
            link.first_clicked_at = now

        # Log the first click as an Activity. Subsequent clicks (re-clicks,
        # bot prefetches) increment click_count but don't spam the timeline.
        if is_first_click and link.contact_id and link.company_id:
            ua = request.headers.get("user-agent", "")[:200]
            ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "")
            short_dest = link.destination_url[:120] + ("…" if len(link.destination_url) > 120 else "")
            db.add(Activity(
                company_id=link.company_id,
                contact_id=link.contact_id,
                activity_type="email_clicked",
                content=f"Clicked link → {short_dest}",
                metadata_json=__import__("json").dumps({
                    "destination_url": link.destination_url,
                    "label": link.label,
                    "email_id": link.email_id,
                    "user_agent": ua,
                    "ip": ip,
                }),
            ))
        await db.commit()

    # 302 redirect with Set-Cookie. visitor_token is the click token itself —
    # Phase 2's beacon will look it up to attribute pageviews back to this
    # contact. SameSite=Lax + 1y expiry + path=/.
    response = RedirectResponse(url=link.destination_url, status_code=302)
    response.set_cookie(
        key="bmp_visitor",
        value=token,
        max_age=31_536_000,  # 1 year
        path="/",
        samesite="lax",
        secure=False,  # cookie is set on prospector.* which is HTTPS in prod, but the
                      # cookie itself rides through the redirect — keep portable
        httponly=False,  # readable by Phase 2 JS snippet on backyardmarketingpros.com
    )
    return response
