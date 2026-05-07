"""
Public click-tracking endpoint — handles /t/{token} from outgoing emails.

Drops the bmp_visitor cookie (Phase 2 will use it to attribute downstream
page views back to this contact), logs an Activity to the timeline, then
302s to the destination URL.

Public route — no auth. Performance-sensitive: every email click hits this.
Keep it under ~30ms by avoiding any unnecessary DB joins.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response, PlainTextResponse, JSONResponse
from sqlalchemy import select, func
from pydantic import BaseModel

from app.database import async_session
from app.models import TrackingLink, PageView, Activity, Contact, Company
from app.config import settings


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
    # Append ?bmp_id=token to the destination so the JS snippet on
    # backyardmarketingpros.com (a different origin from prospector.*)
    # can read it from the URL and drop its own bmp_visitor cookie. The
    # cookie we set here lives on prospector.* and won't be readable by
    # the snippet cross-origin — query string is the bridge.
    sep = "&" if "?" in link.destination_url else "?"
    response = RedirectResponse(url=f"{link.destination_url}{sep}bmp_id={token}", status_code=302)
    response.set_cookie(
        key="bmp_visitor", value=token, max_age=31_536_000, path="/",
        samesite="lax", secure=False, httponly=False,
    )
    return response


# ============================================================
# Phase 2 — JS snippet served from /track.js + pageview beacon
# ============================================================

# The snippet is intentionally tiny (~600 bytes after gzip). Keeps page-load
# impact near zero. Beacon is non-blocking via navigator.sendBeacon.
_TRACK_SNIPPET = """\
(function(){
var API='__API__';
try{
  var p=new URLSearchParams(location.search);
  var id=p.get('bmp_id');
  // First-touch from a tracked email click — drop the cookie on this domain
  // so subsequent page views attribute back to the contact.
  if(id){
    document.cookie='bmp_visitor='+id+';path=/;max-age=31536000;SameSite=Lax';
    // Strip ?bmp_id from the URL bar without reloading (cosmetic)
    if(window.history && history.replaceState){
      var url=new URL(location.href); url.searchParams.delete('bmp_id');
      history.replaceState({},'',url.toString());
    }
  }
  var m=document.cookie.match(/(?:^|;\\s*)bmp_visitor=([^;]+)/);
  if(!m) return;  // no tracked visitor on this device — nothing to log
  var payload=JSON.stringify({
    visitor_token:m[1],
    url:location.href,
    title:(document.title||'').slice(0,500),
    referrer:document.referrer||''
  });
  // sendBeacon is fire-and-forget; ideal for analytics — survives page unload
  if(navigator.sendBeacon){
    navigator.sendBeacon(API+'/api/track/pageview', new Blob([payload],{type:'application/json'}));
  } else {
    fetch(API+'/api/track/pageview',{method:'POST',body:payload,headers:{'Content-Type':'application/json'},keepalive:true}).catch(function(){});
  }
}catch(e){/* swallow — never break the host page */}
})();
"""


@router.get("/track.js")
async def track_snippet():
    """The JS snippet to embed on backyardmarketingpros.com. Drop this in
    a single <script src="https://prospector.backyardmarketingpros.com/track.js" async></script>
    tag and every page view from a tracked visitor lands on the timeline."""
    js = _TRACK_SNIPPET.replace("__API__", settings.public_url.rstrip("/"))
    return Response(
        content=js,
        media_type="application/javascript",
        headers={
            "Cache-Control": "public, max-age=300",  # 5 min — short so changes propagate fast
            "Access-Control-Allow-Origin": "*",  # readable from any origin
        },
    )


class PageViewBeacon(BaseModel):
    visitor_token: str
    url: str
    title: str | None = None
    referrer: str | None = None


@router.post("/api/track/pageview")
async def track_pageview(req: Request):
    """Beacon receiver. CORS-open (no auth — public). Body is JSON sent
    via navigator.sendBeacon from the snippet on bymp.com. We log a row
    + maybe surface a "Hot Lead" Activity if this contact has crossed
    the 3-pages-in-30-min threshold (Phase 3 logic, gated lightly here)."""
    # Parse manually — sendBeacon sets content-type to whatever Blob() got
    try:
        raw = await req.body()
        data = json.loads(raw or b"{}")
    except Exception:
        return JSONResponse({"ok": False}, status_code=400, headers={"Access-Control-Allow-Origin": "*"})

    visitor_token = (data.get("visitor_token") or "").strip()
    url = (data.get("url") or "").strip()[:1000]
    if not visitor_token or not url:
        return JSONResponse({"ok": False}, status_code=400, headers={"Access-Control-Allow-Origin": "*"})

    title = (data.get("title") or "").strip()[:500] or None
    referrer = (data.get("referrer") or "").strip()[:1000] or None
    ua = (req.headers.get("user-agent") or "")[:300] or None
    ip = (req.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (req.client.host if req.client else None)

    async with async_session() as db:
        # Resolve contact + company from the original TrackingLink row
        link = (await db.execute(select(TrackingLink).where(TrackingLink.token == visitor_token))).scalar_one_or_none()
        contact_id = link.contact_id if link else None
        company_id = link.company_id if link else None

        pv = PageView(
            visitor_token=visitor_token,
            contact_id=contact_id,
            company_id=company_id,
            url=url, page_title=title, referrer=referrer,
            user_agent=ua, ip=ip,
        )
        db.add(pv)

        # Phase 3 inline: hot-lead detection — 3+ pages in last 30 min from
        # this same visitor → log a "hot lead" Activity once per session
        await db.flush()
        if contact_id:
            since = datetime.now(timezone.utc) - timedelta(minutes=30)
            recent_count = (await db.execute(
                select(func.count(PageView.id)).where(
                    PageView.visitor_token == visitor_token,
                    PageView.created_at >= since,
                )
            )).scalar_one()
            if recent_count == 3:  # exactly 3 — fire once when threshold crossed
                # Avoid spamming: only one hot_lead activity per 30-min session
                last_hot = (await db.execute(
                    select(Activity).where(
                        Activity.contact_id == contact_id,
                        Activity.activity_type == "hot_lead",
                        Activity.created_at >= since,
                    ).limit(1)
                )).scalar_one_or_none()
                if not last_hot:
                    db.add(Activity(
                        company_id=company_id, contact_id=contact_id,
                        activity_type="hot_lead",
                        content=f"🔥 Active on site — {recent_count} pages in last 30 min",
                        metadata_json=json.dumps({"current_page": url, "visitor_token": visitor_token}),
                    ))
        await db.commit()
    return JSONResponse({"ok": True}, headers={"Access-Control-Allow-Origin": "*"})


# CORS preflight for the beacon
@router.options("/api/track/pageview")
async def track_pageview_options():
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        },
    )
