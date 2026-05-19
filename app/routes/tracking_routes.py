"""
Public click-tracking endpoint — handles /t/{token} from outgoing emails.

Drops the bmp_visitor cookie (Phase 2 will use it to attribute downstream
page views back to this contact), logs an Activity to the timeline, then
302s to the destination URL.

Public route — no auth. Performance-sensitive: every email click hits this.
Keep it under ~30ms by avoiding any unnecessary DB joins.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response, PlainTextResponse, JSONResponse
from sqlalchemy import select, func
from pydantic import BaseModel

from app.database import async_session
log = logging.getLogger("bmp.tracking")
from app.models import TrackingLink, PageView, Activity, Contact, Company
from app.config import settings


router = APIRouter(tags=["tracking"])


# ============================================================
# Per-IP rate limiter for the public beacon endpoint.
#
# Sliding window: keeps the last N timestamps per IP, refuses if the count
# inside the window is over the limit. Lives in-process — fine for our
# single-process deploy. If we ever scale out, swap for Redis-backed.
#
# Defaults sized for legitimate use: 60 events / 60 seconds. A typical
# session is < 20 pageviews + a handful of action events; this cushions
# bursts but blocks anything trying to spray the table.
# ============================================================

_RATE_WINDOW_SEC = 60
_RATE_MAX_PER_WINDOW = 60
_rate_buckets: dict[str, deque] = {}


def _check_rate_limit(ip: str) -> bool:
    """Return True if request is allowed, False if rate-limited."""
    if not ip:
        return True  # can't bucket without an IP — fail open
    now = time.monotonic()
    cutoff = now - _RATE_WINDOW_SEC
    bucket = _rate_buckets.get(ip)
    if bucket is None:
        bucket = deque(maxlen=_RATE_MAX_PER_WINDOW * 2)
        _rate_buckets[ip] = bucket
    # Drop expired entries
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= _RATE_MAX_PER_WINDOW:
        return False
    bucket.append(now)
    # Periodic GC: every ~5000 requests across all IPs, drop empty buckets
    if len(_rate_buckets) > 1000 and len(_rate_buckets) % 100 == 0:
        for k in list(_rate_buckets.keys()):
            b = _rate_buckets[k]
            while b and b[0] < cutoff:
                b.popleft()
            if not b:
                del _rate_buckets[k]
    return True


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
        #
        # Per-email_id dedupe: a single email typically contains the same
        # destination URL wrapped through multiple tracking tokens (logo,
        # body link, signature, footer). Email-client link prefetchers
        # (Apple Mail Privacy Protection, Outlook SafeLinks, Gmail proxy)
        # fire every link the moment a message arrives — generating N
        # "first clicks" on N different tokens within the same email.
        # Collapse those to ONE email_clicked Activity per (email_id, contact).
        if is_first_click and link.contact_id and link.company_id:
            already_clicked = False
            if link.email_id:
                # metadata_json is plain text JSON; the LIKE pattern matches
                # the exact "email_id": N substring that json.dumps produces.
                already_clicked = (await db.execute(
                    select(Activity.id).where(
                        Activity.activity_type == "email_clicked",
                        Activity.contact_id == link.contact_id,
                        Activity.metadata_json.like(f'%"email_id": {link.email_id}%'),
                    ).limit(1)
                )).scalar_one_or_none() is not None

            if not already_clicked:
                ua = request.headers.get("user-agent", "")[:200]
                ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "")
                short_dest = link.destination_url[:120] + ("…" if len(link.destination_url) > 120 else "")
                db.add(Activity(
                    company_id=link.company_id,
                    contact_id=link.contact_id,
                    activity_type="email_clicked",
                    content=f"Clicked link → {short_dest}",
                    metadata_json=json.dumps({
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
    # Defense in depth on the prospector-domain cookie. Nothing actually reads
    # it — the bymp.com snippet picks the token off ?bmp_id= in the URL and
    # writes its OWN bmp_visitor cookie on bymp.com (which DOES need to be
    # JS-readable). So locking this one down to Secure + HttpOnly costs us
    # nothing and removes the cookie from any cross-site script's reach.
    response.set_cookie(
        key="bmp_visitor", value=token, max_age=31_536_000, path="/",
        samesite="lax", secure=True, httponly=True,
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
function makeUuid(){
  // RFC4122 v4 — good enough for visitor identification, no crypto need
  try{
    if(crypto && crypto.randomUUID) return crypto.randomUUID();
  }catch(_){}
  var d=Date.now();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g,function(c){
    var r=(d+Math.random()*16)%16|0;d=Math.floor(d/16);
    return (c==='x'?r:(r&0x3|0x8)).toString(16);
  });
}
function ensureToken(){
  var t=getToken();
  if(!t){
    t='anon-'+makeUuid();
    document.cookie='bmp_visitor='+t+';path=/;max-age=31536000;SameSite=Lax';
  }
  return t;
}
function send(eventType,label,value){
  try{
    var t=ensureToken();
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
  // 1. First-touch from a tracked email click — drop the *known* token
  // from the URL (replaces whatever anonymous UUID we may have stashed).
  var p=new URLSearchParams(location.search);
  var id=p.get('bmp_id');
  if(id){
    document.cookie='bmp_visitor='+id+';path=/;max-age=31536000;SameSite=Lax';
    if(window.history && history.replaceState){
      var u=new URL(location.href); u.searchParams.delete('bmp_id');
      history.replaceState({},'',u.toString());
    }
  }
  // 2. Pageview on load — always fires; ensureToken() will mint an
  //    anonymous UUID if no bmp_visitor cookie exists yet. That UUID
  //    is how we recognize the same anonymous visitor across pageviews
  //    and how we attribute the IP-reveal company match.
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


_TRACK_JS_HEADERS = {
    "Cache-Control": "public, max-age=300",  # 5 min — short so changes propagate fast
    "Access-Control-Allow-Origin": "*",       # readable from any origin
    "X-Content-Type-Options": "nosniff",
}


@router.get("/track.js")
async def track_snippet():
    """The JS snippet to embed on backyardmarketingpros.com. Drop this in
    a single <script src="https://prospector.backyardmarketingpros.com/track.js" async></script>
    tag and every page view from a tracked visitor lands on the timeline."""
    js = _TRACK_SNIPPET.replace("__API__", settings.public_url.rstrip("/"))
    return Response(content=js, media_type="application/javascript", headers=_TRACK_JS_HEADERS)


@router.head("/track.js")
async def track_snippet_head():
    """HEAD handler so link-checkers, monitoring, and HTTP probes don't get
    a 405. Returns the same headers as GET, no body."""
    return Response(status_code=200, media_type="application/javascript", headers=_TRACK_JS_HEADERS)


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
    # Rate-limit by IP — 60 events / 60 sec sliding window. Cheap defense
    # against someone spraying the public endpoint to fill page_views.
    client_ip = (req.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (req.client.host if req.client else "")
    if not _check_rate_limit(client_ip):
        return JSONResponse(
            {"ok": False, "error": "rate_limited"},
            status_code=429,
            headers={"Access-Control-Allow-Origin": "*", "Retry-After": str(_RATE_WINDOW_SEC)},
        )

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

        # Anonymous visitor path — no TrackingLink, but the cookie has
        # a UUID we use to recognize them across pageviews. Look up (or
        # create) a SiteVisitorSession row. If we don't have an IP→
        # company resolution yet, queue one async (no blocking the beacon).
        if not link:
            from app.models import SiteVisitorSession
            session = (await db.execute(
                select(SiteVisitorSession).where(SiteVisitorSession.bvid == visitor_token)
            )).scalar_one_or_none()
            now = datetime.now(timezone.utc)
            if not session:
                session = SiteVisitorSession(
                    bvid=visitor_token, ip=ip, user_agent=ua,
                    pageview_count=0,
                    first_seen_at=now, last_seen_at=now,
                )
                db.add(session)
                await db.flush()
                # Fire-and-forget IP reveal; results land on session row.
                if ip:
                    asyncio.create_task(_resolve_session_async(session.id, ip))
            else:
                session.last_seen_at = now
                # If IP changed (mobile hop), don't blow away the prior
                # resolved company — keep it sticky to the bvid cookie.
                if ip and not session.ip:
                    session.ip = ip
                # If we still don't have a resolution and have an IP,
                # try again (rate-limited at the resolver level).
                if not session.resolved_at and ip:
                    asyncio.create_task(_resolve_session_async(session.id, ip))
            session.pageview_count = (session.pageview_count or 0) + 1
            # Attribute the pageview to the resolved company if we have one.
            company_id = session.resolved_company_id or None

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


# ============================================================
# Async IP → company resolution. Fire-and-forget from the beacon
# handler — never blocks the beacon. Opens its own DB session.
# ============================================================

async def _resolve_session_async(session_id: int, ip: str) -> None:
    """Look up the IP via the resolver service, then backfill the
    site_visitor_session row + match-or-create a Company record if the
    domain looks viable. Safe to call multiple times for the same
    session — short-circuits if already resolved."""
    try:
        from app.services.visitor_resolver import resolve_ip
        from app.models import SiteVisitorSession, Company

        reveal = await resolve_ip(ip)
        if reveal is None:
            return

        async with async_session() as db:
            session = (await db.execute(
                select(SiteVisitorSession).where(SiteVisitorSession.id == session_id)
            )).scalar_one_or_none()
            if not session:
                return
            session.resolved_at = datetime.now(timezone.utc)
            session.is_isp_ip = bool(reveal.get("is_isp_ip"))
            session.country = reveal.get("country")
            session.region = reveal.get("region")
            session.city = reveal.get("city")
            session.resolved_company_name = reveal.get("company_name")
            session.resolved_domain = reveal.get("domain")

            # Try to match to an existing Company by domain. We DO NOT
            # auto-create companies for ISP IPs (residential noise).
            if reveal.get("domain") and not reveal.get("is_isp_ip"):
                existing = (await db.execute(
                    select(Company).where(Company.domain == reveal["domain"]).limit(1)
                )).scalar_one_or_none()
                if existing:
                    session.resolved_company_id = existing.id
                # Don't auto-create here — surface in the Site Visitors
                # UI with a "Add as Company" button so the user picks
                # which visits matter (avoids polluting the CRM with
                # one-off bot / partner / vendor visits).

            await db.commit()
    except Exception as e:
        log.warning(f"visitor resolve failed for session {session_id} ip {ip}: {e}")
