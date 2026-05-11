"""Missive iframe sidebar integration.

Two endpoints:

  GET /integrations/missive/sidebar
      Public HTML page (no auth required to load the shell — auth
      happens client-side via Missive.initiateCallback). Loaded by
      Missive as an iframe; uses missive.js to subscribe to
      change:conversations, then calls /api/integrations/context with
      the BDR's JWT.

  GET /integrations/missive/auth
      Handles the Missive OAuth-style callback. Missive opens this URL
      in a new tab with a ?redirectTo= query param; we present a small
      login form, and once the BDR submits a valid prospector.bymp.com
      JWT (or logs in inline), we 302 to redirectTo?token=<jwt>.
      Missive's SDK intercepts the redirect, closes the tab, and
      resolves initiateCallback() with the query params.

Security headers:
  - These routes MUST NOT send X-Frame-Options: DENY or a
    `frame-ancestors` CSP — both break iframe embedding inside Missive.
  - SecurityHeadersMiddleware has a per-path opt-out for /integrations/
    so iframe-friendly responses go out clean.
"""
from __future__ import annotations
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import User

router = APIRouter(prefix="/integrations/missive", tags=["missive"])
log = logging.getLogger("bmp.missive")


# ============================================================
# Sidebar iframe (public HTML shell)
# ============================================================

@router.get("/sidebar", response_class=HTMLResponse)
async def missive_sidebar() -> HTMLResponse:
    """The iframe Missive loads. All real logic runs client-side via
    missive.js; we just serve the shell."""
    app_url = settings.public_url.rstrip("/")
    audit_url = settings.audit_public_url.rstrip("/")
    return HTMLResponse(_render_sidebar_html(app_url=app_url, audit_url=audit_url))


# ============================================================
# OAuth-style auth callback (Missive.initiateCallback target)
# ============================================================

@router.get("/auth", response_class=HTMLResponse)
async def missive_auth_page(request: Request) -> HTMLResponse:
    """Opened in a new tab by Missive.initiateCallback. Shows a login
    form whose submit hits prospector.bymp.com's /api/auth/login, then
    redirects to `redirectTo?token=<jwt>` so Missive captures the
    token and closes the tab automatically."""
    redirect_to = request.query_params.get("redirectTo", "").strip()
    app_url = settings.public_url.rstrip("/")
    return HTMLResponse(_render_auth_html(redirect_to=redirect_to, app_url=app_url))


# ============================================================
# Authenticated identity probe (called from the sidebar JS to verify
# the stored JWT is still valid + return the user's name/role)
# ============================================================

@router.get("/me")
async def missive_me(
    user: User = Depends(get_current_user),
) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name or "",
        "full_name": user.full_name,
        "role": user.role,
    }


# ============================================================
# HTML renderers
# ============================================================

def _render_sidebar_html(app_url: str, audit_url: str) -> str:
    # Inline app config so the JS has the right hosts without another
    # network round-trip on every iframe load.
    auth_url = f"{app_url}/integrations/missive/auth"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Prospector — Context</title>
  <script src="https://integrations.missiveapp.com/missive.js"></script>
  <link href="https://integrations.missiveapp.com/missive.css" rel="stylesheet">
  <style>
    :root {{ --bmp-orange: #E65100; --bmp-green: #1B5E20; --bmp-cream: #FFF8F0; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 13px; color: #1a1a1a; background: white; }}
    .container {{ padding: 14px; }}
    .empty {{ color: #888; padding: 20px; text-align: center; font-size: 13px; }}
    .empty button {{ background: var(--bmp-orange); color: white; border: 0; padding: 8px 14px; border-radius: 6px; cursor: pointer; margin-top: 10px; font-weight: 600; }}
    .section {{ margin-bottom: 14px; padding-bottom: 14px; border-bottom: 1px solid #eee; }}
    .section:last-child {{ border-bottom: 0; }}
    .label {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; color: #888; margin-bottom: 4px; font-weight: 600; }}
    h2 {{ margin: 0 0 4px; font-size: 16px; color: var(--bmp-green); }}
    h3 {{ margin: 0; font-size: 14px; }}
    .pill {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }}
    .pill-status-new {{ background: #eee; color: #555; }}
    .pill-status-pursuing {{ background: #fff4e0; color: #b06a00; }}
    .pill-status-sequencing {{ background: #e3f2fd; color: #1565c0; }}
    .pill-status-contacted {{ background: #e8eaf6; color: #303f9f; }}
    .pill-status-replied {{ background: #c8e6c9; color: #1b5e20; }}
    .pill-status-qualified {{ background: var(--bmp-cream); color: var(--bmp-orange); }}
    .pill-status-converted {{ background: #43a047; color: white; }}
    .pill-status-not_interested {{ background: #ffebee; color: #c62828; }}
    .pill-tier-hot {{ background: var(--bmp-orange); color: white; }}
    .pill-tier-warm {{ background: #fff4e0; color: #b06a00; }}
    .pill-tier-cold {{ background: #e3f2fd; color: #1565c0; }}
    .row {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; margin: 4px 0; }}
    a {{ color: var(--bmp-green); text-decoration: none; word-break: break-all; }}
    a:hover {{ text-decoration: underline; }}
    .activity {{ font-size: 12px; padding: 6px 0; border-top: 1px dashed #f0f0f0; }}
    .activity:first-child {{ border-top: 0; }}
    .activity-type {{ font-weight: 600; color: var(--bmp-green); font-size: 11px; text-transform: uppercase; }}
    .activity-when {{ color: #999; font-size: 10px; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    button {{ font-family: inherit; }}
    .btn {{ background: var(--bmp-orange); color: white; border: 0; padding: 7px 12px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; flex: 1; min-width: 100px; }}
    .btn.secondary {{ background: #f4f4f4; color: #333; }}
    .btn:hover {{ opacity: 0.9; }}
    .spinner {{ display: inline-block; width: 16px; height: 16px; border: 2px solid #eee; border-top-color: var(--bmp-orange); border-radius: 50%; animation: spin 0.8s linear infinite; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    code {{ font-family: ui-monospace, Menlo, monospace; font-size: 11px; background: #f5f5f5; padding: 1px 5px; border-radius: 3px; word-break: break-all; }}
  </style>
</head>
<body>
  <div id="root" class="container">
    <div class="empty"><div class="spinner"></div><div style="margin-top:8px">Loading…</div></div>
  </div>

  <script>
    const APP_URL = {app_url!r};
    const AUDIT_URL = {audit_url!r};
    const AUTH_URL = {auth_url!r};
    const root = document.getElementById('root');
    let _currentContext = null;
    let _jwt = null;

    function escapeHtml(s) {{
      return (s || '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
    }}

    function fmtRel(iso) {{
      if (!iso) return '';
      try {{
        const sec = (Date.now() - new Date(iso).getTime()) / 1000;
        if (sec < 60) return 'just now';
        if (sec < 3600) return Math.round(sec/60) + 'm ago';
        if (sec < 86400) return Math.round(sec/3600) + 'h ago';
        return Math.round(sec/86400) + 'd ago';
      }} catch (e) {{ return ''; }}
    }}

    async function jwtAuthFetch(path, opts = {{}}) {{
      opts.headers = Object.assign({{}}, opts.headers || {{}}, {{
        'Authorization': 'Bearer ' + _jwt,
        'Content-Type': 'application/json',
      }});
      const r = await fetch(APP_URL + path, opts);
      if (r.status === 401) {{
        _jwt = null;
        await Missive.storeSet('prospector_jwt', null);
        return null;
      }}
      if (!r.ok) return null;
      return r.json();
    }}

    async function ensureLogin() {{
      _jwt = await Missive.storeGet('prospector_jwt');
      if (_jwt) {{
        // Verify it's still valid
        const me = await jwtAuthFetch('/integrations/missive/me');
        if (me && me.email) return true;
      }}
      // Need to (re-)auth. Render the gate.
      renderLoginGate();
      return false;
    }}

    function renderLoginGate() {{
      root.innerHTML = `
        <div class="empty">
          <h2>Prospector</h2>
          <p style="margin: 12px 0; color: #555">Sign in to see CRM context for this conversation.</p>
          <button onclick="doLogin()">Sign in to Prospector</button>
        </div>`;
    }}

    async function doLogin() {{
      try {{
        const resp = await Missive.initiateCallback(AUTH_URL);
        if (resp && resp.token) {{
          _jwt = resp.token;
          await Missive.storeSet('prospector_jwt', _jwt);
          // Re-trigger the current conversation render
          handleChangeConversations(_lastConversationIds || []);
        }} else {{
          Missive.alert({{ title: 'Sign-in failed', message: 'No token returned. Try again.' }});
        }}
      }} catch (e) {{
        Missive.alert({{ title: 'Sign-in error', message: String(e && e.message || e) }});
      }}
    }}

    let _lastConversationIds = [];

    async function handleChangeConversations(conversationIds) {{
      _lastConversationIds = conversationIds || [];
      if (!_jwt) {{
        renderLoginGate();
        return;
      }}
      if (!conversationIds || conversationIds.length === 0) {{
        root.innerHTML = '<div class="empty">No conversation selected.</div>';
        return;
      }}
      if (conversationIds.length > 1) {{
        root.innerHTML = '<div class="empty">Multiple conversations selected.</div>';
        return;
      }}

      root.innerHTML = '<div class="empty"><div class="spinner"></div><div style="margin-top:8px">Loading context…</div></div>';

      // Pull the conversation's latest message to extract the prospect email
      let conv;
      try {{
        const conversations = await Missive.fetchConversations(conversationIds);
        conv = conversations && conversations[0];
      }} catch (e) {{
        root.innerHTML = `<div class="empty">Could not load conversation: ${{escapeHtml(String(e))}}</div>`;
        return;
      }}

      const latest = conv && conv.latest_message;
      // Choose the prospect address: the from_field if it's NOT one of OUR users,
      // otherwise the to_fields. Best heuristic without per-user data — operator
      // can refine later.
      let probeEmail = '';
      if (latest && latest.from_field && latest.from_field.address) {{
        probeEmail = latest.from_field.address;
      }} else if (latest && latest.to_fields && latest.to_fields[0]) {{
        probeEmail = latest.to_fields[0].address;
      }} else if (conv && conv.authors && conv.authors[0]) {{
        probeEmail = conv.authors[0].address;
      }}

      if (!probeEmail) {{
        root.innerHTML = '<div class="empty">No email address on this conversation.</div>';
        return;
      }}

      const ctx = await jwtAuthFetch('/api/integrations/context?email=' + encodeURIComponent(probeEmail));
      if (!ctx) {{
        root.innerHTML = '<div class="empty">Failed to look up context.</div>';
        return;
      }}
      _currentContext = ctx;

      if (!ctx.found) {{
        root.innerHTML = `
          <div class="section">
            <div class="label">Not in Prospector</div>
            <h3>${{escapeHtml(probeEmail)}}</h3>
            <p style="color:#666;margin:8px 0 12px">This contact hasn't been added yet.</p>
            <div class="actions">
              <button class="btn" onclick="window.open(APP_URL + '/?add_email=' + encodeURIComponent('${{probeEmail}}'), '_blank')">Add to Prospector</button>
            </div>
          </div>`;
        return;
      }}

      renderContext(ctx);
    }}

    function renderContext(ctx) {{
      const c = ctx.contact || {{}};
      const co = ctx.company || {{}};
      const seq = ctx.sequence;
      const audit = ctx.audit;

      const statusPill = co.status ? `<span class="pill pill-status-${{co.status}}">${{escapeHtml(co.status)}}</span>` : '';
      const tierPill   = co.lead_score_tier ? `<span class="pill pill-tier-${{co.lead_score_tier}}" title="Lead score ${{co.lead_score}}">${{escapeHtml(co.lead_score_tier)}} · ${{co.lead_score}}</span>` : '';

      let activityHtml = '';
      if (ctx.activities && ctx.activities.length) {{
        activityHtml = `
          <div class="section">
            <div class="label">Recent activity</div>
            ${{ctx.activities.map(a => `
              <div class="activity">
                <span class="activity-type">${{escapeHtml(a.type)}}</span>
                <span class="activity-when">· ${{escapeHtml(fmtRel(a.at))}}</span>
                <div style="color:#555">${{escapeHtml(a.content)}}</div>
              </div>
            `).join('')}}
          </div>`;
      }}

      let seqHtml = '';
      if (seq) {{
        const next = seq.next_step;
        seqHtml = `
          <div class="section">
            <div class="label">Sequence</div>
            <div>${{seq.sent_steps}}/${{seq.total_steps}} steps sent</div>
            ${{next ? `<div style="color:#555;margin-top:4px;font-size:12px">Next: ${{escapeHtml(next.type)}} · ${{escapeHtml(next.subject || '')}}${{next.scheduled_at ? ' · ' + escapeHtml(fmtRel(next.scheduled_at)) : ''}}</div>` : '<div style="color:#888;margin-top:4px;font-size:12px">No further steps queued.</div>'}}
          </div>`;
      }}

      let auditHtml = '';
      if (audit) {{
        auditHtml = `
          <div class="section">
            <div class="label">AI Findability audit</div>
            <div class="row">
              <div><strong>${{escapeHtml(audit.grade || '?')}}</strong> · ${{audit.ai_findability_score}}/100 · ${{audit.view_count}} view${{audit.view_count===1?'':'s'}}</div>
              <a href="${{audit.url}}" target="_blank">View →</a>
            </div>
          </div>`;
      }}

      const appCompanyUrl = APP_URL + '/?company_id=' + co.id;
      root.innerHTML = `
        <div class="section">
          <div class="row">
            <div>
              <h2>${{escapeHtml(co.name || 'Unknown company')}}</h2>
              <div style="color:#666;margin-top:2px">${{escapeHtml(c.full_name || c.email || '')}}${{c.title ? ' · ' + escapeHtml(c.title) : ''}}</div>
            </div>
          </div>
          <div style="margin-top:6px">${{statusPill}} ${{tierPill}}</div>
        </div>

        ${{seqHtml}}
        ${{auditHtml}}
        ${{activityHtml}}

        <div class="section actions">
          <button class="btn" onclick="window.open('${{appCompanyUrl}}', '_blank')">Open in Prospector</button>
          ${{audit ? '' : `<button class="btn secondary" onclick="generateAuditNow()">Generate audit</button>`}}
          ${{seq ? `<button class="btn secondary" onclick="pauseSequence()">Pause sequence</button>` : ''}}
        </div>
      `;
    }}

    async function generateAuditNow() {{
      const co = _currentContext && _currentContext.company;
      if (!co) return;
      const r = await jwtAuthFetch('/api/companies/' + co.id + '/audit', {{ method: 'POST' }});
      if (r && r.url) {{
        Missive.alert({{ title: 'Audit generated', message: 'Grade ' + (r.overall_grade || '?') + ' · ' + r.ai_findability_score + '/100' }});
        handleChangeConversations(_lastConversationIds);
      }} else {{
        Missive.alert({{ title: 'Audit failed', message: 'Could not generate the report.' }});
      }}
    }}

    async function pauseSequence() {{
      const c = _currentContext && _currentContext.contact;
      if (!c) return;
      const r = await jwtAuthFetch('/api/sequences/pause/' + c.id, {{ method: 'POST' }});
      if (r) {{
        Missive.alert({{ title: 'Sequence paused', message: 'No further steps will fire for this contact.' }});
        handleChangeConversations(_lastConversationIds);
      }} else {{
        Missive.alert({{ title: 'Pause failed', message: 'Could not pause the sequence.' }});
      }}
    }}

    // Boot
    (async function boot() {{
      const signedIn = await ensureLogin();
      Missive.on('change:conversations', handleChangeConversations, {{ retroactive: true }});
    }})();
  </script>
</body>
</html>"""


def _render_auth_html(redirect_to: str, app_url: str) -> str:
    """Page opened in a new tab by Missive.initiateCallback. The BDR
    signs in to Prospector using the same email/password they use on
    the main app — we mint a JWT, then redirect to `redirectTo?token=
    <jwt>` so Missive captures it and closes the tab."""
    from html import escape as _e
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sign in to Prospector</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f7f5; min-height: 100vh; display: flex; align-items: center; justify-content: center; }}
    .card {{ background: white; padding: 32px; border-radius: 12px; box-shadow: 0 4px 24px rgba(0,0,0,0.08); width: 380px; }}
    h1 {{ margin: 0 0 4px; color: #1B5E20; font-size: 20px; }}
    p {{ margin: 0 0 18px; color: #666; font-size: 13px; }}
    label {{ display: block; font-size: 12px; font-weight: 600; color: #444; margin: 12px 0 4px; }}
    input {{ width: 100%; padding: 9px 10px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; box-sizing: border-box; }}
    button {{ background: #E65100; color: white; border: 0; padding: 11px; border-radius: 6px; font-size: 14px; font-weight: 600; cursor: pointer; width: 100%; margin-top: 18px; }}
    .err {{ background: #ffebee; color: #c62828; padding: 10px; border-radius: 6px; font-size: 12px; margin-top: 12px; display: none; }}
    .err.show {{ display: block; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Sign in to Prospector</h1>
    <p>Used to authorize the Missive sidebar. Same credentials as prospector.backyardmarketingpros.com.</p>
    <form id="f" autocomplete="on">
      <label for="email">Email</label>
      <input id="email" name="email" type="email" required autocomplete="username">
      <label for="password">Password</label>
      <input id="password" name="password" type="password" required autocomplete="current-password">
      <div class="err" id="err"></div>
      <button type="submit" id="submit">Sign in</button>
    </form>
  </div>
  <script>
    const APP_URL = {app_url!r};
    const REDIRECT_TO = {redirect_to!r};

    document.getElementById('f').addEventListener('submit', async (e) => {{
      e.preventDefault();
      const err = document.getElementById('err');
      const btn = document.getElementById('submit');
      err.classList.remove('show');
      btn.disabled = true;
      btn.textContent = 'Signing in…';
      try {{
        // /api/auth/login expects OAuth2PasswordRequestForm — i.e.
        // application/x-www-form-urlencoded with `username` + `password`
        const fd = new URLSearchParams();
        fd.set('username', document.getElementById('email').value);
        fd.set('password', document.getElementById('password').value);
        const r = await fetch(APP_URL + '/api/auth/login', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
          body: fd.toString(),
        }});
        const data = await r.json();
        if (!r.ok || !data.access_token) {{
          err.textContent = data.detail || 'Sign-in failed.';
          err.classList.add('show');
          btn.disabled = false;
          btn.textContent = 'Sign in';
          return;
        }}
        if (!REDIRECT_TO) {{
          err.textContent = 'Missing redirect target — open the sidebar from inside Missive.';
          err.classList.add('show');
          btn.disabled = false;
          btn.textContent = 'Sign in';
          return;
        }}
        const sep = REDIRECT_TO.indexOf('?') === -1 ? '?' : '&';
        window.location.href = REDIRECT_TO + sep + 'token=' + encodeURIComponent(data.access_token);
      }} catch (e2) {{
        err.textContent = 'Network error: ' + (e2 && e2.message || e2);
        err.classList.add('show');
        btn.disabled = false;
        btn.textContent = 'Sign in';
      }}
    }});
  </script>
</body>
</html>"""
