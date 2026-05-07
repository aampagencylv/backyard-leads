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
function getToken(){
  var m=document.cookie.match(/(?:^|;\\s*)bmp_visitor=([^;]+)/);
  return m?m[1]:null;
}
function send(eventType,label,value){
  try{
    var t=getToken();
    if(!t) return;
    var payload=JSON.stringify({
      visitor_token:t,
      url:location.href,
      title:(document.title||'').slice(0,500),
      referrer:document.referrer||'',
      event_type:eventType||'pageview',
      event_label:(label||'').toString().slice(0,200),
      event_value:(value||'').toString().slice(0,2000)
    });
    if(navigator.sendBeacon){
      navigator.sendBeacon(API+'/api/track/pageview', new Blob([payload],{type:'application/json'}));
    } else {
      fetch(API+'/api/track/pageview',{method:'POST',body:payload,headers:{'Content-Type':'application/json'},keepalive:true}).catch(function(){});
    }
  }catch(e){/* swallow */}
}
try{
  // 1. First-touch from a tracked email click — drop cookie + scrub bmp_id
  var p=new URLSearchParams(location.search);
  var id=p.get('bmp_id');
  if(id){
    document.cookie='bmp_visitor='+id+';path=/;max-age=31536000;SameSite=Lax';
    if(window.history && history.replaceState){
      var u=new URL(location.href); u.searchParams.delete('bmp_id');
      history.replaceState({},'',u.toString());
    }
  }
  // 2. Pageview on load
  send('pageview','','');

  // 3. Form submissions — capture phase so we don't block the form
  document.addEventListener('submit',function(e){
    try{
      var f=e.target; if(!f||f.tagName!=='FORM') return;
      var label=f.id||f.getAttribute('name')||f.getAttribute('data-bmp-event')||'form';
      send('form_submit',label,(f.action||location.href));
    }catch(_){}
  },true);

  // 4. Outbound + tel + mailto + custom button clicks — single delegated listener
  document.addEventListener('click',function(e){
    try{
      // Walk up to find the closest <a> or [data-bmp-event]
      var el=e.target;
      while(el && el!==document.body){
        if(el.dataset && el.dataset.bmpEvent){
          send('custom', el.dataset.bmpEvent, (el.href||el.textContent||'').slice(0,200));
          return;
        }
        if(el.tagName==='A' && el.href){
          var href=el.href;
          if(href.indexOf('tel:')===0){ send('tel_click', href.slice(4), href); return; }
          if(href.indexOf('mailto:')===0){ send('mailto_click', href.slice(7), href); return; }
          // Outbound = different hostname than current page
          try{
            var u=new URL(href);
            if(u.hostname && u.hostname!==location.hostname){
              send('outbound_click', u.hostname, href);
              return;
            }
          }catch(_){}
        }
        el=el.parentElement;
      }
    }catch(_){}
  },true);
}catch(e){/* never break the host page */}
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


@router.post("/api/track/pageview")
async def track_pageview(req: Request):
    """Beacon receiver. CORS-open (no auth — public). Same endpoint handles
    pageviews AND actions (form submit, outbound click, tel/mailto tap, custom
    button click) via the event_type field on the payload.

    Hot-lead tiering:
      - HIGH-INTENT actions (form_submit, tel_click, mailto_click) → instant
        hot-lead Activity, deduped to one per 24 hr per contact
      - 3+ pageviews in 30 min → "active on site" hot-lead, deduped per session
      - outbound_click + custom events → logged but don't auto-fire hot-lead
        (BDR can scan the timeline; spamming on every Calendly link click would
        be noisy)
    """
    try:
        raw = await req.body()
        data = json.loads(raw or b"{}")
    except Exception:
        return JSONResponse({"ok": False}, status_code=400, headers={"Access-Control-Allow-Origin": "*"})

    visitor_token = (data.get("visitor_token") or "").strip()
    url = (data.get("url") or "").strip()[:1000]
    if not visitor_token or not url:
        return JSONResponse({"ok": False}, status_code=400, headers={"Access-Control-Allow-Origin": "*"})

    event_type  = (data.get("event_type")  or "pageview").strip()[:30].lower()
    event_label = (data.get("event_label") or "").strip()[:200] or None
    event_value = (data.get("event_value") or "").strip()[:2000] or None
    title = (data.get("title") or "").strip()[:500] or None
    referrer = (data.get("referrer") or "").strip()[:1000] or None
    ua = (req.headers.get("user-agent") or "")[:300] or None
    ip = (req.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (req.client.host if req.client else None)

    HIGH_INTENT = {"form_submit", "tel_click", "mailto_click"}
    # Whitelist to prevent garbage event_type values from filling the index
    KNOWN_EVENTS = {"pageview", "form_submit", "outbound_click", "tel_click", "mailto_click", "custom"}
    if event_type not in KNOWN_EVENTS:
        event_type = "custom"

    async with async_session() as db:
        link = (await db.execute(select(TrackingLink).where(TrackingLink.token == visitor_token))).scalar_one_or_none()
        contact_id = link.contact_id if link else None
        company_id = link.company_id if link else None

        pv = PageView(
            visitor_token=visitor_token,
            contact_id=contact_id,
            company_id=company_id,
            url=url, page_title=title, referrer=referrer,
            user_agent=ua, ip=ip,
            event_type=event_type, event_label=event_label, event_value=event_value,
        )
        db.add(pv)
        await db.flush()

        if contact_id:
            now = datetime.now(timezone.utc)

            # HIGH-INTENT actions → instant hot lead, deduped per 24 hr
            if event_type in HIGH_INTENT:
                since_24h = now - timedelta(hours=24)
                recent_high = (await db.execute(
                    select(Activity).where(
                        Activity.contact_id == contact_id,
                        Activity.activity_type == "hot_lead",
                        Activity.content.like("%[high-intent]%"),
                        Activity.created_at >= since_24h,
                    ).limit(1)
                )).scalar_one_or_none()
                if not recent_high:
                    nice_event = {
                        "form_submit":   f"submitted a form ({event_label or 'unnamed'})",
                        "tel_click":     f"tapped a phone link ({event_label or 'unknown number'})",
                        "mailto_click":  f"clicked an email link ({event_label or 'unknown'})",
                    }.get(event_type, event_type)
                    db.add(Activity(
                        company_id=company_id, contact_id=contact_id,
                        activity_type="hot_lead",
                        content=f"🔥 [high-intent] {nice_event} — on {url}",
                        metadata_json=json.dumps({
                            "event_type": event_type,
                            "event_label": event_label,
                            "event_value": event_value,
                            "page_url": url,
                            "visitor_token": visitor_token,
                        }),
                    ))

            # PAGEVIEW threshold → "active on site", deduped per 30-min session
            elif event_type == "pageview":
                since = now - timedelta(minutes=30)
                recent_pv_count = (await db.execute(
                    select(func.count(PageView.id)).where(
                        PageView.visitor_token == visitor_token,
                        PageView.event_type == "pageview",
                        PageView.created_at >= since,
                    )
                )).scalar_one()
                if recent_pv_count == 3:
                    last_hot = (await db.execute(
                        select(Activity).where(
                            Activity.contact_id == contact_id,
                            Activity.activity_type == "hot_lead",
                            Activity.content.like("%pages in last%"),
                            Activity.created_at >= since,
                        ).limit(1)
                    )).scalar_one_or_none()
                    if not last_hot:
                        db.add(Activity(
                            company_id=company_id, contact_id=contact_id,
                            activity_type="hot_lead",
                            content=f"🔥 Active on site — {recent_pv_count} pages in last 30 min",
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
