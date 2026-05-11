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
    .action-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px; margin-top: 10px; }}
    .action-btn {{ background: #f7f7f7; border: 1px solid #e3e3e3; color: #333; padding: 8px 10px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; text-align: left; transition: background 0.1s; }}
    .action-btn:hover {{ background: #eee; }}
    .note {{ font-size: 12px; padding: 8px; background: #fffbe6; border-left: 3px solid #f5b800; border-radius: 4px; margin-top: 6px; }}
    .note:first-child {{ margin-top: 0; }}
    .task-row {{ display: flex; align-items: flex-start; padding: 6px 0; border-bottom: 1px dashed #f0f0f0; }}
    .task-row:last-child {{ border-bottom: 0; }}
    .contact-row {{ display: flex; align-items: center; padding: 6px 0; gap: 8px; border-bottom: 1px dashed #f0f0f0; }}
    .contact-row:last-child {{ border-bottom: 0; }}
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
    let _lastConv = null;          // The current Missive conversation object
    let _lastProbeEmail = '';      // The address we used to look up context
    let _teamEmails = new Set();   // Lowercased team-member emails from /api/integrations/context

    // Pick the most likely PROSPECT email out of a conversation. Skips
    // any address that belongs to a team member (cached on first fetch).
    function pickProspectEmail(conv) {{
      if (!conv) return '';
      const candidates = [];
      const latest = conv.latest_message;
      if (latest) {{
        if (latest.from_field && latest.from_field.address) candidates.push(latest.from_field.address);
        (latest.to_fields  || []).forEach(f => f && f.address && candidates.push(f.address));
        (latest.cc_fields  || []).forEach(f => f && f.address && candidates.push(f.address));
      }}
      (conv.authors || []).forEach(a => a && a.address && candidates.push(a.address));
      // Prefer the first candidate that's NOT a team member
      for (const addr of candidates) {{
        const a = (addr || '').trim().toLowerCase();
        if (!a) continue;
        if (_teamEmails.has(a)) continue;
        return addr;
      }}
      // No external address found — fall back to whatever's there
      return candidates[0] || '';
    }}

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
      _lastConv = conv;

      // First pass uses whatever team emails we already cached; the
      // backend will return a fresh list which we'll persist for next time.
      let probeEmail = pickProspectEmail(conv);

      if (!probeEmail) {{
        root.innerHTML = '<div class="empty">No email address on this conversation.</div>';
        return;
      }}

      _lastProbeEmail = probeEmail;

      // Pass conversation_id so the backend persists the contact↔conv link
      const ctx = await jwtAuthFetch(
        '/api/integrations/context?email=' + encodeURIComponent(probeEmail)
        + '&conversation_id=' + encodeURIComponent(conv.id || '')
      );
      if (!ctx) {{
        root.innerHTML = '<div class="empty">Failed to look up context.</div>';
        return;
      }}
      // Refresh the team-emails cache from the backend response so next
      // conversation switch has the correct heuristic immediately.
      if (Array.isArray(ctx.team_emails)) {{
        _teamEmails = new Set(ctx.team_emails.map(e => (e || '').trim().toLowerCase()));
        // If we initially guessed wrong (matched a team member), retry
        // with the prospect-filtered pick.
        const better = pickProspectEmail(conv);
        if (better && better.toLowerCase() !== probeEmail.toLowerCase()) {{
          _lastProbeEmail = better;
          const ctx2 = await jwtAuthFetch(
            '/api/integrations/context?email=' + encodeURIComponent(better)
            + '&conversation_id=' + encodeURIComponent(conv.id || '')
          );
          if (ctx2) {{
            _currentContext = ctx2;
            if (!ctx2.found) {{ renderNotFound(better, ctx2); return; }}
            renderContext(ctx2);
            return;
          }}
        }}
      }}
      _currentContext = ctx;

      if (!ctx.found) {{
        renderNotFound(probeEmail, ctx);
        return;
      }}

      renderContext(ctx);
    }}

    function renderNotFound(probeEmail, ctx) {{
      root.innerHTML = `
        <div class="section">
          <div class="label">Not in Prospector</div>
          <h3>${{escapeHtml(probeEmail)}}</h3>
          <p style="color:#666;margin:8px 0 12px">This contact hasn't been added yet.</p>
          <div class="actions">
            <button class="btn" onclick="quickAdd('${{escapeHtml(probeEmail)}}')">Quick add</button>
            <button class="btn secondary" onclick="window.open(APP_URL + '/?add_email=' + encodeURIComponent('${{probeEmail}}'), '_blank')">Open in Prospector</button>
          </div>
        </div>`;
    }}

    async function quickAdd(emailAddr) {{
      // Pull a sensible default for the name from the Missive conversation
      let firstName = '', lastName = '';
      try {{
        const latest = _lastConv && _lastConv.latest_message;
        const f = latest && latest.from_field;
        if (f && f.address && f.address.toLowerCase() === emailAddr.toLowerCase()) {{
          const parts = (f.name || '').trim().split(/\\s+/);
          firstName = parts[0] || '';
          lastName = parts.slice(1).join(' ') || '';
        }}
      }} catch (e) {{}}
      try {{
        const fields = await Missive.openForm({{
          name: 'Add prospect to Prospector',
          fields: [
            {{ name: 'first_name', label: 'First name', initial: firstName }},
            {{ name: 'last_name',  label: 'Last name',  initial: lastName }},
            {{ name: 'company_name', label: 'Company (optional)', initial: '' }},
            {{ name: 'title', label: 'Title (optional)', initial: '' }},
          ],
          buttons: [{{ name: 'add', label: 'Add to Prospector' }}],
          autoClose: true,
        }});
        const get = (k) => (fields.find(x => x.name === k) || {{}}).value || '';
        const r = await jwtAuthFetch('/api/integrations/sidebar/quick-add', {{
          method: 'POST',
          body: JSON.stringify({{
            email: emailAddr,
            first_name: get('first_name'),
            last_name: get('last_name'),
            company_name: get('company_name'),
            title: get('title'),
          }}),
        }});
        if (r && (r.created || r.contact_id)) {{
          Missive.alert({{ title: 'Added to Prospector', message: emailAddr + ' is now in your CRM.' }});
          // Re-render with the now-found record
          handleChangeConversations(_lastConversationIds);
        }} else {{
          Missive.alert({{ title: 'Add failed', message: 'Could not add the contact.' }});
        }}
      }} catch (e) {{
        // openForm rejects on cancel — silently ignore
      }}
    }}

    function renderContext(ctx) {{
      const c = ctx.contact || {{}};
      const co = ctx.company || {{}};
      const seq = ctx.sequence;
      const audit = ctx.audit;
      const missiveOk = !!ctx.missive_configured;

      const statusPill = co.status ? `<span class="pill pill-status-${{co.status}}">${{escapeHtml(co.status)}}</span>` : '';
      const tierPill   = co.lead_score_tier ? `<span class="pill pill-tier-${{co.lead_score_tier}}" title="Lead score ${{co.lead_score}}">${{escapeHtml(co.lead_score_tier)}} · ${{co.lead_score}}</span>` : '';
      const appCompanyUrl = APP_URL + '/?company_id=' + co.id;

      // Big-button contact actions
      const actionBtns = [];
      if (c.phone) {{
        actionBtns.push(`<button class="action-btn" title="Call ${{escapeHtml(c.phone)}}" onclick="callNow('${{escapeHtml(c.phone)}}', ${{c.id}})">📞 Call</button>`);
        actionBtns.push(`<button class="action-btn" title="Send iMessage to ${{escapeHtml(c.phone)}}" onclick="quickIMessage()">💬 iMessage</button>`);
      }}
      if (c.email) {{
        actionBtns.push(`<button class="action-btn" title="Copy email" onclick="copyEmail('${{escapeHtml(c.email)}}')">✉️ Copy email</button>`);
      }}
      if (c.linkedin_url) {{
        actionBtns.push(`<button class="action-btn" title="Open LinkedIn profile" onclick="window.open('${{escapeHtml(c.linkedin_url)}}', '_blank')">💼 LinkedIn</button>`);
      }}
      const actionBtnsHtml = actionBtns.length ? `<div class="action-grid">${{actionBtns.join('')}}</div>` : '';

      // Inline contact details row
      const detailParts = [];
      if (c.phone)   detailParts.push(`<a href="tel:${{escapeHtml(c.phone)}}" style="color:#444">${{escapeHtml(c.phone)}}</a>`);
      if (c.email)   detailParts.push(`<a href="mailto:${{escapeHtml(c.email)}}" style="color:#444">${{escapeHtml(c.email)}}</a>`);
      const detailHtml = detailParts.length ? `<div style="font-size:11px;color:#888;margin-top:6px;line-height:1.6">${{detailParts.join('<br>')}}</div>` : '';

      // Status quick-change dropdown
      const statusOpts = (ctx.status_options || []).map(s =>
        `<option value="${{s}}" ${{s === co.status ? 'selected' : ''}}>${{escapeHtml(s)}}</option>`
      ).join('');

      // Engagement summary
      const engagementBits = [];
      if (ctx.last_opened_at)  engagementBits.push(`👁️ Last open ${{escapeHtml(fmtRel(ctx.last_opened_at))}}`);
      if (ctx.last_clicked_at) engagementBits.push(`🖱️ Last click ${{escapeHtml(fmtRel(ctx.last_clicked_at))}}`);
      const engagementHtml = engagementBits.length ?
        `<div style="font-size:11px;color:#666;margin-top:4px">${{engagementBits.join(' · ')}}</div>` : '';

      // Sequence
      let seqHtml = '';
      if (seq) {{
        const next = seq.next_step;
        const pct = seq.total_steps ? Math.round(seq.sent_steps * 100 / seq.total_steps) : 0;
        seqHtml = `
          <div class="section">
            <div class="label">Sequence</div>
            <div style="display:flex;justify-content:space-between;align-items:center;font-size:13px">
              <span>${{seq.sent_steps}} / ${{seq.total_steps}} sent</span>
              <span style="color:#888;font-size:11px">${{pct}}%</span>
            </div>
            <div style="background:#eee;border-radius:4px;height:4px;margin-top:4px;overflow:hidden">
              <div style="background:var(--bmp-orange);height:100%;width:${{pct}}%"></div>
            </div>
            ${{next ? `<div style="color:#555;margin-top:8px;font-size:12px">Next: <strong>${{escapeHtml(next.type)}}</strong> · ${{escapeHtml(next.subject || '')}}${{next.scheduled_at ? ' · ' + escapeHtml(fmtRel(next.scheduled_at)) : ''}}</div>` : '<div style="color:#888;margin-top:8px;font-size:12px">No further steps queued.</div>'}}
          </div>`;
      }}

      // Audit
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

      // Pinned notes — shown prominently
      let notesHtml = '';
      const pinned = ctx.pinned_notes || [];
      if (pinned.length) {{
        notesHtml = `
          <div class="section">
            <div class="label">📝 Notes & Calls</div>
            ${{pinned.map(n => `
              <div class="note">
                <span class="activity-type">${{escapeHtml(n.type)}}</span>
                <span class="activity-when">· ${{escapeHtml(fmtRel(n.at))}}</span>
                <div style="color:#333;margin-top:2px;white-space:pre-wrap;word-break:break-word">${{escapeHtml(n.content)}}</div>
              </div>
            `).join('')}}
          </div>`;
      }}

      // Open tasks
      let tasksHtml = '';
      const tasks = ctx.open_tasks || [];
      if (tasks.length) {{
        tasksHtml = `
          <div class="section">
            <div class="label">📋 Open tasks</div>
            ${{tasks.map(t => `
              <div class="task-row">
                <input type="checkbox" onchange="completeTask(${{t.id}})" style="margin-right:6px;cursor:pointer">
                <div style="flex:1">
                  <div style="font-size:12px">${{escapeHtml(t.description || '')}}</div>
                  ${{t.due_date ? `<div style="font-size:11px;color:#888">Due ${{escapeHtml(fmtRel(t.due_date))}}</div>` : ''}}
                </div>
              </div>
            `).join('')}}
          </div>`;
      }}

      // Other contacts at the company
      let otherContactsHtml = '';
      const others = ctx.other_contacts || [];
      if (others.length) {{
        otherContactsHtml = `
          <div class="section">
            <div class="label">👥 Other contacts at ${{escapeHtml(co.name || 'this company')}}</div>
            ${{others.map(oc => `
              <div class="contact-row">
                <div style="flex:1;min-width:0">
                  <div style="font-size:12px;font-weight:600;display:flex;align-items:center;gap:4px">
                    ${{escapeHtml(oc.full_name || oc.email || '?')}}
                    ${{oc.is_primary ? '<span style="font-size:9px;background:#fff4e0;color:#b06a00;padding:1px 5px;border-radius:3px;font-weight:600">PRIMARY</span>' : ''}}
                  </div>
                  <div style="font-size:11px;color:#888">${{escapeHtml(oc.title || '')}}${{oc.title && oc.email ? ' · ' : ''}}${{escapeHtml(oc.email || '')}}</div>
                </div>
                ${{oc.phone ? `<a href="tel:${{escapeHtml(oc.phone)}}" title="Call" style="text-decoration:none;padding:3px 6px;background:#f0f0f0;border-radius:4px;font-size:11px">📞</a>` : ''}}
              </div>
            `).join('')}}
          </div>`;
      }}

      // Recent emails
      let recentEmailsHtml = '';
      if (ctx.recent_emails && ctx.recent_emails.length) {{
        recentEmailsHtml = `
          <div class="section">
            <div class="label">Recent sequence steps</div>
            ${{ctx.recent_emails.map(em => {{
              const when = em.sent_at ? fmtRel(em.sent_at) : 'queued';
              const flags = [];
              if (em.bounced_at)    flags.push(`<span style="color:#c62828">bounced</span>`);
              if (em.complained_at) flags.push(`<span style="color:#c62828">spam</span>`);
              if (em.open_count)    flags.push(`<span style="color:#1565c0">${{em.open_count}}× open</span>`);
              if (!em.is_sent)      flags.push(`<span style="color:#888">queued</span>`);
              return `
                <div class="activity">
                  <div style="display:flex;justify-content:space-between;gap:6px">
                    <div style="flex:1;min-width:0">
                      <span class="activity-type">${{escapeHtml(em.step_type || 'email')}}</span>
                      <span class="activity-when">· ${{escapeHtml(when)}}</span>
                      ${{flags.length ? ` · ${{flags.join(' · ')}}` : ''}}
                    </div>
                  </div>
                  <div style="color:#444;font-size:12px;text-overflow:ellipsis;overflow:hidden;white-space:nowrap">${{escapeHtml(em.subject || '')}}</div>
                </div>`;
            }}).join('')}}
          </div>`;
      }}

      // Full activity timeline (non-note items only — notes are pinned above)
      let activityHtml = '';
      const noteTypes = new Set(['note', 'call', 'call_logged', 'meeting']);
      const otherActivities = (ctx.activities || []).filter(a => !noteTypes.has(a.type));
      if (otherActivities.length) {{
        activityHtml = `
          <div class="section">
            <div class="label">Activity timeline</div>
            ${{otherActivities.map(a => `
              <div class="activity">
                <span class="activity-type">${{escapeHtml(a.type)}}</span>
                <span class="activity-when">· ${{escapeHtml(fmtRel(a.at))}}</span>
                <div style="color:#555">${{escapeHtml(a.content)}}</div>
              </div>
            `).join('')}}
          </div>`;
      }}

      root.innerHTML = `
        <div class="section">
          <div class="row">
            <div style="flex:1;min-width:0">
              <h2 style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{escapeHtml(co.name || 'Unknown company')}}</h2>
              <div style="color:#666;margin-top:2px;font-size:12px">${{escapeHtml(c.full_name || c.email || '')}}${{c.title ? ' · ' + escapeHtml(c.title) : ''}}</div>
            </div>
            <a href="${{appCompanyUrl}}" target="_blank" title="Open in Prospector" style="font-size:18px;text-decoration:none">↗</a>
          </div>
          <div style="margin-top:8px;display:flex;gap:6px;align-items:center;flex-wrap:wrap">
            ${{statusPill}} ${{tierPill}}
            <select onchange="setStatus(this.value)" style="margin-left:auto;font-size:11px;padding:3px 6px;border:1px solid #ddd;border-radius:4px;background:white;cursor:pointer">${{statusOpts}}</select>
          </div>
          ${{detailHtml}}
          ${{engagementHtml}}
          ${{actionBtnsHtml}}
        </div>

        <div class="section">
          <div class="label">Quick note</div>
          <textarea id="quick-note" placeholder="Add a note… (⌘+Enter to save)" rows="2"
            style="width:100%;border:1px solid #ddd;border-radius:6px;padding:6px 8px;font-size:12px;font-family:inherit;resize:vertical;box-sizing:border-box"></textarea>
          <div style="display:flex;gap:6px;margin-top:6px">
            <button class="btn secondary" onclick="saveInlineNote('note')" style="flex:1">Save note</button>
            <button class="btn secondary" onclick="saveInlineNote('call_logged')" style="flex:1">Save as call</button>
          </div>
        </div>

        ${{notesHtml}}
        ${{tasksHtml}}
        ${{seqHtml}}
        ${{auditHtml}}
        ${{otherContactsHtml}}
        ${{recentEmailsHtml}}
        ${{activityHtml}}

        <div class="section actions">
          ${{seq && seq.next_step ? `<button class="btn" onclick="sendNextStep()">Send next step now</button>` : ''}}
          ${{audit ? '' : `<button class="btn secondary" onclick="generateAuditNow()">Generate audit</button>`}}
          ${{seq ? `<button class="btn secondary" onclick="pauseSequence()">Pause sequence</button>` : ''}}
          ${{missiveOk ? `<button class="btn secondary" onclick="syncMissiveTag()">🏷️ Sync label</button>` : ''}}
        </div>
      `;

      // Wire up Cmd+Enter on the inline note textarea
      const ta = document.getElementById('quick-note');
      if (ta) {{
        ta.addEventListener('keydown', (e) => {{
          if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {{
            e.preventDefault();
            saveInlineNote('note');
          }}
        }});
      }}
    }}

    function callNow(phone, contactId) {{
      // Pop the dialer in the main Prospector app via deep-link.
      // The app picks up ?dial=<phone> at boot and triggers its
      // existing Twilio dialer (browser or bridge mode, per user pref).
      const url = APP_URL + '/?dial=' + encodeURIComponent(phone || '') + '&contact_id=' + (contactId || '');
      window.open(url, 'prospector-dialer');
    }}

    async function saveInlineNote(kind) {{
      const ta = document.getElementById('quick-note');
      if (!ta) return;
      const text = (ta.value || '').trim();
      if (!text) return;
      const c  = _currentContext && _currentContext.contact;  if (!c) return;
      const co = _currentContext && _currentContext.company;  if (!co) return;
      ta.disabled = true;
      const r = await jwtAuthFetch('/api/integrations/sidebar/log-activity', {{
        method: 'POST',
        body: JSON.stringify({{ contact_id: c.id, company_id: co.id, text, activity_type: kind }}),
      }});
      ta.disabled = false;
      if (r && r.id) {{
        ta.value = '';
        handleChangeConversations(_lastConversationIds);
      }} else {{
        Missive.alert({{ title: 'Save failed', message: 'Could not save the note.' }});
      }}
    }}

    async function setStatus(newStatus) {{
      const c  = _currentContext && _currentContext.contact;
      const co = _currentContext && _currentContext.company;
      if (!co) return;
      const r = await jwtAuthFetch('/api/integrations/sidebar/set-status', {{
        method: 'POST',
        body: JSON.stringify({{ company_id: co.id, contact_id: c ? c.id : null, new_status: newStatus }}),
      }});
      if (r && r.ok) {{
        const msg = r.label_applied ? `Status: ${{r.status}}. Missive label "${{r.label_applied}}" applied.` : `Status: ${{r.status}}.`;
        Missive.alert({{ title: 'Updated', message: msg }});
        handleChangeConversations(_lastConversationIds);
      }} else {{
        Missive.alert({{ title: 'Update failed', message: 'Could not change status.' }});
      }}
    }}

    async function completeTask(taskId) {{
      const r = await jwtAuthFetch('/api/integrations/sidebar/complete-task', {{
        method: 'POST',
        body: JSON.stringify({{ task_id: taskId }}),
      }});
      if (r && r.ok) {{
        handleChangeConversations(_lastConversationIds);
      }}
    }}

    async function quickIMessage() {{
      const c = _currentContext && _currentContext.contact;
      if (!c || !c.phone) return;
      try {{
        const fields = await Missive.openForm({{
          name: 'Send iMessage',
          fields: [{{ name: 'body', label: `To ${{c.phone}}`, initial: '' }}],
          buttons: [{{ name: 'send', label: 'Send iMessage' }}],
          autoClose: true,
        }});
        const body = ((fields.find(x => x.name === 'body') || {{}}).value || '').trim();
        if (!body) return;
        const r = await jwtAuthFetch('/api/integrations/sidebar/send-imessage', {{
          method: 'POST',
          body: JSON.stringify({{ contact_id: c.id, body }}),
        }});
        if (r && r.ok) {{
          Missive.alert({{ title: 'iMessage sent', message: 'Logged to the activity timeline.' }});
          handleChangeConversations(_lastConversationIds);
        }} else if (r) {{
          Missive.alert({{ title: 'Not sent', message: r.reason || 'Unknown error' }});
        }} else {{
          Missive.alert({{ title: 'Send failed', message: 'Could not send the iMessage.' }});
        }}
      }} catch (e) {{ /* cancelled */ }}
    }}

    async function copyEmail(addr) {{
      try {{
        await Missive.writeToClipboard(addr);
        Missive.alert({{ title: 'Copied', message: addr }});
      }} catch (e) {{}}
    }}

    async function _logActivityForm(kindLabel, defaultKind) {{
      const c  = _currentContext && _currentContext.contact;  if (!c) return;
      const co = _currentContext && _currentContext.company;  if (!co) return;
      try {{
        const fields = await Missive.openForm({{
          name: kindLabel,
          fields: [
            {{ name: 'text', label: 'Notes', initial: '' }},
          ],
          buttons: [{{ name: 'save', label: 'Save to Prospector' }}],
          autoClose: true,
        }});
        const text = ((fields.find(x => x.name === 'text') || {{}}).value || '').trim();
        if (!text) return;
        const r = await jwtAuthFetch('/api/integrations/sidebar/log-activity', {{
          method: 'POST',
          body: JSON.stringify({{
            contact_id: c.id,
            company_id: co.id,
            text: text,
            activity_type: defaultKind,
          }}),
        }});
        if (r && r.id) {{
          Missive.alert({{ title: 'Logged', message: kindLabel + ' saved to the Prospector timeline.' }});
          handleChangeConversations(_lastConversationIds);
        }} else {{
          Missive.alert({{ title: 'Save failed', message: 'Could not log the activity.' }});
        }}
      }} catch (e) {{
        // user cancelled
      }}
    }}
    function logNote() {{ _logActivityForm('Log a note', 'note'); }}
    function logCall() {{ _logActivityForm('Log a call', 'call_logged'); }}

    async function sendNextStep() {{
      const c = _currentContext && _currentContext.contact;
      if (!c) return;
      const r = await jwtAuthFetch('/api/integrations/sidebar/send-next-step', {{
        method: 'POST',
        body: JSON.stringify({{ contact_id: c.id }}),
      }});
      if (r && r.fired) {{
        Missive.alert({{ title: 'Step fired', message: 'Sent step #' + r.step_id + ' (' + (r.step_type || 'email') + ').' }});
        handleChangeConversations(_lastConversationIds);
      }} else if (r) {{
        Missive.alert({{ title: 'No step fired', message: r.reason || 'No pending step.' }});
      }} else {{
        Missive.alert({{ title: 'Send failed', message: 'Could not fire the next step.' }});
      }}
    }}

    async function syncMissiveTag() {{
      const c = _currentContext && _currentContext.contact;
      if (!c || !_lastConv || !_lastConv.id) return;
      const r = await jwtAuthFetch('/api/integrations/sidebar/missive-sync-tag', {{
        method: 'POST',
        body: JSON.stringify({{
          contact_id: c.id,
          conversation_id: _lastConv.id,
        }}),
      }});
      if (r && r.ok) {{
        Missive.alert({{ title: 'Label synced', message: 'Applied "' + (r.label_name || r.status_applied) + '" to this conversation.' }});
      }} else if (r && r.error) {{
        Missive.alert({{ title: 'Sync failed', message: r.error }});
      }} else {{
        Missive.alert({{ title: 'Sync failed', message: 'Could not apply label.' }});
      }}
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
