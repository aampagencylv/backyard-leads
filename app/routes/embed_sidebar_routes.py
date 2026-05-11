"""Generic embedded sidebar — same CRM panel as the Missive sidebar,
but loadable from any host iframe (Chrome extension, web overlay,
future BYO embeds). No vendor SDK dependency.

Contract:
  Parent embeds <iframe src="…/integrations/embed/sidebar?t=<jwt>&email=<addr>">.
  The iframe authenticates via the URL ?t=<jwt> token (one-time on
  boot — never echoed back into the URL after load), then renders
  the standard /api/integrations/context payload for the email.

  Parent can switch threads/contexts without reloading the iframe by
  posting a message to the iframe window:

      iframe.contentWindow.postMessage(
        { type: 'set_email', email: 'new@prospect.com', conversation_id: 'optional' },
        '*'
      );

  The iframe debounces these and refetches context. Origin check is
  loose ('*') because the parent can be from any host (Gmail, LinkedIn,
  the Chrome extension itself).

Security headers: same exemption pattern as Missive — no
X-Frame-Options DENY, no `frame-ancestors` CSP, so the iframe can
embed inside Gmail / LinkedIn / Missive / any extension popup.
"""
from __future__ import annotations
import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.config import settings

router = APIRouter(prefix="/integrations/embed", tags=["embed"])
log = logging.getLogger("bmp.embed_sidebar")


@router.get("/sidebar", response_class=HTMLResponse)
async def embed_sidebar() -> HTMLResponse:
    app_url = settings.public_url.rstrip("/")
    audit_url = settings.audit_public_url.rstrip("/")
    return HTMLResponse(_render_embed_sidebar(app_url=app_url, audit_url=audit_url))


def _render_embed_sidebar(app_url: str, audit_url: str) -> str:
    """Standalone sidebar — same look as the Missive panel but
    bootstrapped via URL/postMessage instead of the Missive SDK.

    The content of this template is intentionally a near-copy of the
    Missive sidebar's renderContext + action handlers. Two files share
    UX but are independently maintainable; if either drifts we'll
    extract the shared bits later.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Prospector — Context</title>
  <style>
    :root {{ --bmp-orange: #E65100; --bmp-green: #1B5E20; --bmp-cream: #FFF8F0; color-scheme: light; }}
    * {{ box-sizing: border-box; }}
    html, body {{ background: white; color: #1a1a1a; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 13px; }}
    input, textarea, select {{ background: white; color: #1a1a1a; }}
    .container {{ padding: 14px; }}
    .empty {{ color: #888; padding: 20px; text-align: center; font-size: 13px; }}
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
    .btn {{ background: var(--bmp-orange); color: white; border: 0; padding: 9px 12px; border-radius: 7px; font-size: 12px; font-weight: 600; cursor: pointer; flex: 1; min-width: 100px; box-shadow: 0 1px 2px rgba(0,0,0,0.06); transition: transform 0.06s ease, box-shadow 0.1s ease, opacity 0.1s; }}
    .btn:hover {{ opacity: 0.92; box-shadow: 0 2px 5px rgba(0,0,0,0.12); }}
    .btn:active {{ transform: translateY(1px); }}
    .btn.secondary {{ background: #f4f4f4; color: #333; box-shadow: inset 0 0 0 1px #e3e3e3; }}
    .btn.secondary:hover {{ background: #ececec; }}

    .action-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-top: 12px; }}
    .action-btn {{ display: flex; align-items: center; gap: 8px; background: white; color: #1a1a1a; border: 1px solid #dfdfdf; padding: 11px 13px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; text-align: left; box-shadow: 0 1px 2px rgba(0,0,0,0.04); transition: transform 0.06s ease, box-shadow 0.12s ease, border-color 0.12s ease, background 0.12s ease; }}
    .action-btn .icon {{ font-size: 17px; line-height: 1; flex-shrink: 0; }}
    .action-btn:hover {{ border-color: #c5c5c5; box-shadow: 0 2px 6px rgba(0,0,0,0.09); }}
    .action-btn:active {{ transform: translateY(1px); }}
    .action-btn.primary {{ background: linear-gradient(135deg, var(--bmp-orange) 0%, #FF7A36 100%); color: white; border-color: transparent; box-shadow: 0 2px 6px rgba(230, 81, 0, 0.35); }}
    .action-btn.imessage {{ background: linear-gradient(135deg, #007AFF 0%, #00A2FF 100%); color: white; border-color: transparent; box-shadow: 0 2px 6px rgba(0, 122, 255, 0.32); }}
    .action-btn.linkedin {{ background: #0A66C2; color: white; border-color: transparent; box-shadow: 0 2px 6px rgba(10, 102, 194, 0.28); }}
    .action-btn.schedule {{ background: linear-gradient(135deg, var(--bmp-green) 0%, #2E7D32 100%); color: white; border-color: transparent; box-shadow: 0 2px 6px rgba(27, 94, 32, 0.32); }}
    .action-btn.task {{ background: #fff8f0; border-color: #ffd9b3; color: #b05500; }}

    .due-btn {{ background: white; border: 1px solid #ddd; color: #444; padding: 6px 6px; border-radius: 5px; font-size: 11px; font-weight: 600; cursor: pointer; }}
    .due-btn:hover {{ background: #f7f7f7; }}
    .due-btn.active {{ background: var(--bmp-orange); color: white; border-color: var(--bmp-orange); }}

    .note {{ font-size: 12px; padding: 8px; background: #fffbe6; border-left: 3px solid #f5b800; border-radius: 4px; margin-top: 6px; }}
    .task-row {{ display: flex; align-items: flex-start; padding: 6px 0; border-bottom: 1px dashed #f0f0f0; }}
    .task-row:last-child {{ border-bottom: 0; }}
    .contact-row {{ display: flex; align-items: center; padding: 6px 0; gap: 8px; border-bottom: 1px dashed #f0f0f0; }}
    .contact-row:last-child {{ border-bottom: 0; }}
    .spinner {{ display: inline-block; width: 16px; height: 16px; border: 2px solid #eee; border-top-color: var(--bmp-orange); border-radius: 50%; animation: spin 0.8s linear infinite; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    code {{ font-family: ui-monospace, Menlo, monospace; font-size: 11px; background: #f5f5f5; padding: 1px 5px; border-radius: 3px; }}
  </style>
</head>
<body>
  <div id="root" class="container">
    <div class="empty"><div class="spinner"></div><div style="margin-top:8px">Loading…</div></div>
  </div>
  <div id="toast"
       style="position:fixed;left:50%;bottom:18px;transform:translateX(-50%) translateY(40px);
              background:#1a1a1a;color:white;padding:10px 16px;border-radius:8px;
              font-size:13px;font-weight:500;box-shadow:0 4px 16px rgba(0,0,0,0.25);
              opacity:0;transition:transform 0.2s ease, opacity 0.2s ease;pointer-events:none;
              z-index:9999;max-width:320px;text-align:center"></div>

  <script>
    const APP_URL = {app_url!r};
    const AUDIT_URL = {audit_url!r};
    const root = document.getElementById('root');
    let _currentContext = null;
    let _currentEmail = '';
    let _currentConversationId = '';
    let _jwt = '';
    let _newTaskDueDays = 1;

    // ---------- Boot: read URL params ----------
    (function bootFromUrl() {{
      const p = new URLSearchParams(window.location.search || '');
      _jwt = (p.get('t') || '').trim();
      _currentEmail = (p.get('email') || '').trim();
      _currentConversationId = (p.get('conversation_id') || '').trim();
      // Clean the URL so the token doesn't sit in the iframe location
      try {{ history.replaceState(null, '', window.location.pathname); }} catch (e) {{}}
    }})();

    // ---------- Listen for context switches from the parent ----------
    window.addEventListener('message', (ev) => {{
      const msg = ev && ev.data;
      if (!msg || typeof msg !== 'object') return;
      if (msg.type === 'set_email') {{
        const newEmail = (msg.email || '').trim();
        if (newEmail && newEmail.toLowerCase() !== _currentEmail.toLowerCase()) {{
          _currentEmail = newEmail;
          _currentConversationId = (msg.conversation_id || '').trim();
          loadContext();
        }}
      }} else if (msg.type === 'set_token') {{
        _jwt = (msg.token || '').trim();
        loadContext();
      }}
    }});

    // ---------- Helpers ----------
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

    let _toastTimer = null;
    function toast(msg, kind) {{
      const el = document.getElementById('toast');
      if (!el) return;
      el.textContent = msg;
      el.style.background = kind === 'error' ? '#b71c1c' : (kind === 'success' ? '#1b5e20' : '#1a1a1a');
      el.style.opacity = '1';
      el.style.transform = 'translateX(-50%) translateY(0)';
      if (_toastTimer) clearTimeout(_toastTimer);
      _toastTimer = setTimeout(() => {{
        el.style.opacity = '0';
        el.style.transform = 'translateX(-50%) translateY(40px)';
      }}, kind === 'error' ? 5000 : 2400);
    }}

    async function authFetch(path, opts = {{}}) {{
      if (!_jwt) {{
        renderLoginGate();
        return null;
      }}
      opts.headers = Object.assign({{}}, opts.headers || {{}}, {{
        'Authorization': 'Bearer ' + _jwt,
        'Content-Type': 'application/json',
      }});
      let r;
      try {{
        r = await fetch(APP_URL + path, opts);
      }} catch (e) {{
        console.error('[embed] fetch failed', path, e);
        toast('Network error', 'error');
        return null;
      }}
      if (r.status === 401) {{
        _jwt = '';
        // Tell the parent — it owns the auth store (chrome.storage etc)
        try {{ window.parent && window.parent.postMessage({{ type: 'auth_expired' }}, '*'); }} catch (e) {{}}
        renderLoginGate();
        return null;
      }}
      if (!r.ok) {{
        const errText = await r.text().catch(() => '(no body)');
        console.error('[embed]', path, 'returned', r.status, errText);
        toast('Server returned ' + r.status, 'error');
        return null;
      }}
      try {{ return await r.json(); }} catch (e) {{ return null; }}
    }}

    function renderLoginGate() {{
      root.innerHTML = `
        <div class="empty">
          <h2>Prospector</h2>
          <p style="margin: 12px 0; color: #555">Not signed in. Click the extension icon and sign in to load CRM context here.</p>
        </div>`;
    }}

    // ---------- Main context loader ----------
    async function loadContext() {{
      if (!_jwt) {{ renderLoginGate(); return; }}
      if (!_currentEmail) {{
        root.innerHTML = '<div class="empty">Open an email thread to see CRM context here.</div>';
        return;
      }}
      root.innerHTML = '<div class="empty"><div class="spinner"></div><div style="margin-top:8px">Loading context for ' + escapeHtml(_currentEmail) + '…</div></div>';
      const qs = '?email=' + encodeURIComponent(_currentEmail) +
        (_currentConversationId ? '&conversation_id=' + encodeURIComponent(_currentConversationId) : '');
      const ctx = await authFetch('/api/integrations/context' + qs);
      if (!ctx) return;
      _currentContext = ctx;
      if (!ctx.found) {{
        renderNotFound(_currentEmail);
        return;
      }}
      renderContext(ctx);
    }}

    function renderNotFound(email) {{
      root.innerHTML = `
        <div class="section">
          <div class="label">Not in Prospector</div>
          <h3>${{escapeHtml(email)}}</h3>
          <p style="color:#666;margin:8px 0 12px">This contact hasn't been added yet.</p>
          <div class="actions">
            <button class="btn" onclick="quickAddInline()">Quick add</button>
            <button class="btn secondary" onclick="openInProspector()">Open in Prospector</button>
          </div>
        </div>
        <div id="quick-add-form" class="section" style="display:none;background:#fff8f0;border:1px solid #ffd9b3;border-radius:8px">
          <div class="label">➕ Add ${{escapeHtml(email)}}</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
            <input id="qa-first" placeholder="First name" style="padding:6px 8px;border:1px solid #ddd;border-radius:5px;font-size:12px">
            <input id="qa-last"  placeholder="Last name"  style="padding:6px 8px;border:1px solid #ddd;border-radius:5px;font-size:12px">
          </div>
          <input id="qa-company" placeholder="Company (optional)" style="margin-top:6px;padding:6px 8px;border:1px solid #ddd;border-radius:5px;font-size:12px;width:100%;box-sizing:border-box">
          <input id="qa-title" placeholder="Title (optional)" style="margin-top:6px;padding:6px 8px;border:1px solid #ddd;border-radius:5px;font-size:12px;width:100%;box-sizing:border-box">
          <div style="display:flex;gap:6px;margin-top:10px">
            <button class="btn" onclick="submitQuickAdd()" style="flex:1">Add to Prospector</button>
            <button class="btn secondary" onclick="document.getElementById('quick-add-form').style.display='none'">Cancel</button>
          </div>
        </div>`;
    }}

    function quickAddInline() {{
      const f = document.getElementById('quick-add-form');
      if (f) {{ f.style.display = 'block'; setTimeout(() => document.getElementById('qa-first').focus(), 30); }}
    }}

    async function submitQuickAdd() {{
      const get = id => (document.getElementById(id)?.value || '').trim();
      const r = await authFetch('/api/integrations/sidebar/quick-add', {{
        method: 'POST',
        body: JSON.stringify({{
          email: _currentEmail,
          first_name: get('qa-first'),
          last_name: get('qa-last'),
          company_name: get('qa-company'),
          title: get('qa-title'),
        }}),
      }});
      if (r && (r.created || r.contact_id)) {{
        toast('Added to Prospector', 'success');
        loadContext();
      }}
    }}

    function openInProspector() {{
      const co = _currentContext && _currentContext.company;
      const url = co ? (APP_URL + '/?company_id=' + co.id) : (APP_URL + '/?add_email=' + encodeURIComponent(_currentEmail));
      window.open(_deepLinkUrl(url.replace(APP_URL, '')), 'prospector');
    }}

    function _deepLinkUrl(query) {{
      const q = query.startsWith('?') ? query : (query.startsWith('/') ? query : '?' + query);
      const sep = q.indexOf('?') >= 0 ? '&' : '?';
      return APP_URL + q + (_jwt ? sep + 't=' + encodeURIComponent(_jwt) : '');
    }}

    function renderContext(ctx) {{
      const c = ctx.contact || {{}};
      const co = ctx.company || {{}};
      const seq = ctx.sequence;
      const audit = ctx.audit;

      const statusPill = co.status ? `<span class="pill pill-status-${{co.status}}">${{escapeHtml(co.status)}}</span>` : '';
      const tierPill   = co.lead_score_tier ? `<span class="pill pill-tier-${{co.lead_score_tier}}" title="Lead score ${{co.lead_score}}">${{escapeHtml(co.lead_score_tier)}} · ${{co.lead_score}}</span>` : '';

      const actionBtns = [];
      if (c.phone) {{
        actionBtns.push(`<button class="action-btn primary" onclick="callNow('${{escapeHtml(c.phone)}}', ${{c.id}})"><span class="icon">📞</span><span>Call</span></button>`);
        actionBtns.push(`<button class="action-btn imessage" onclick="quickIMessage()"><span class="icon">💬</span><span>iMessage</span></button>`);
      }}
      actionBtns.push(`<button class="action-btn schedule" onclick="scheduleMeeting()"><span class="icon">📅</span><span>Schedule meeting</span></button>`);
      actionBtns.push(`<button class="action-btn task" onclick="toggleAddTask()"><span class="icon">📋</span><span>Add task</span></button>`);
      if (c.linkedin_url) {{
        actionBtns.push(`<button class="action-btn linkedin" onclick="window.open('${{escapeHtml(c.linkedin_url)}}', '_blank')"><span class="icon">💼</span><span>LinkedIn</span></button>`);
      }}
      if (c.email) {{
        actionBtns.push(`<button class="action-btn" onclick="copyEmail('${{escapeHtml(c.email)}}')"><span class="icon">✉️</span><span>Copy email</span></button>`);
      }}
      const actionBtnsHtml = `<div class="action-grid">${{actionBtns.join('')}}</div>`;

      const detailParts = [];
      if (c.phone) detailParts.push(`<a href="tel:${{escapeHtml(c.phone)}}" style="color:#444">${{escapeHtml(c.phone)}}</a>`);
      if (c.email) detailParts.push(`<a href="mailto:${{escapeHtml(c.email)}}" style="color:#444">${{escapeHtml(c.email)}}</a>`);
      const detailHtml = detailParts.length ? `<div style="font-size:11px;color:#888;margin-top:6px;line-height:1.6">${{detailParts.join('<br>')}}</div>` : '';

      const statusOpts = (ctx.status_options || []).map(s =>
        `<option value="${{s}}" ${{s === co.status ? 'selected' : ''}}>${{escapeHtml(s)}}</option>`
      ).join('');

      const engagementBits = [];
      if (ctx.last_opened_at) engagementBits.push(`👁️ Last open ${{escapeHtml(fmtRel(ctx.last_opened_at))}}`);
      if (ctx.last_clicked_at) engagementBits.push(`🖱️ Last click ${{escapeHtml(fmtRel(ctx.last_clicked_at))}}`);
      const engagementHtml = engagementBits.length ? `<div style="font-size:11px;color:#666;margin-top:4px">${{engagementBits.join(' · ')}}</div>` : '';

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
            <div style="background:#eee;border-radius:4px;height:4px;margin-top:4px;overflow:hidden"><div style="background:var(--bmp-orange);height:100%;width:${{pct}}%"></div></div>
            ${{next ? `<div style="color:#555;margin-top:8px;font-size:12px">Next: <strong>${{escapeHtml(next.type)}}</strong> · ${{escapeHtml(next.subject || '')}}${{next.scheduled_at ? ' · ' + escapeHtml(fmtRel(next.scheduled_at)) : ''}}</div>` : '<div style="color:#888;margin-top:8px;font-size:12px">No further steps queued.</div>'}}
          </div>`;
      }}

      const auditHtml = audit ? `
        <div class="section">
          <div class="label">AI Findability audit</div>
          <div class="row">
            <div><strong>${{escapeHtml(audit.grade || '?')}}</strong> · ${{audit.ai_findability_score}}/100 · ${{audit.view_count}} view${{audit.view_count===1?'':'s'}}</div>
            <a href="${{audit.url}}" target="_blank">View →</a>
          </div>
        </div>` : '';

      const pinned = ctx.pinned_notes || [];
      const notesHtml = pinned.length ? `
        <div class="section">
          <div class="label">📝 Notes & Calls</div>
          ${{pinned.map(n => `
            <div class="note">
              <span class="activity-type">${{escapeHtml(n.type)}}</span>
              <span class="activity-when">· ${{escapeHtml(fmtRel(n.at))}}</span>
              <div style="color:#333;margin-top:2px;white-space:pre-wrap;word-break:break-word">${{escapeHtml(n.content)}}</div>
            </div>`).join('')}}
        </div>` : '';

      const tasks = ctx.open_tasks || [];
      const tasksHtml = tasks.length ? `
        <div class="section">
          <div class="label">📋 Open tasks</div>
          ${{tasks.map(t => {{
            const overdue = t.due_date && (new Date(t.due_date).getTime() < Date.now());
            return `
              <div class="task-row">
                <input type="checkbox" onchange="completeTask(${{t.id}})" style="margin-right:6px;cursor:pointer">
                <div style="flex:1;min-width:0">
                  <div style="font-size:12px">${{escapeHtml(t.description || '')}}</div>
                  ${{t.due_date ? `<div style="font-size:11px;color:${{overdue ? '#c62828' : '#888'}}">Due ${{escapeHtml(fmtRel(t.due_date))}}${{overdue ? ' · OVERDUE' : ''}}</div>` : ''}}
                </div>
              </div>`;
          }}).join('')}}
        </div>` : '';

      const others = ctx.other_contacts || [];
      const otherContactsHtml = others.length ? `
        <div class="section">
          <div class="label">👥 Other contacts at ${{escapeHtml(co.name || 'this company')}}</div>
          ${{others.map(oc => `
            <div class="contact-row">
              <div style="flex:1;min-width:0">
                <div style="font-size:12px;font-weight:600">${{escapeHtml(oc.full_name || oc.email || '?')}}${{oc.is_primary ? ' <span style="font-size:9px;background:#fff4e0;color:#b06a00;padding:1px 5px;border-radius:3px;font-weight:600">PRIMARY</span>' : ''}}</div>
                <div style="font-size:11px;color:#888">${{escapeHtml(oc.title || '')}}${{oc.title && oc.email ? ' · ' : ''}}${{escapeHtml(oc.email || '')}}</div>
              </div>
              ${{oc.phone ? `<a href="tel:${{escapeHtml(oc.phone)}}" title="Call" style="text-decoration:none;padding:3px 6px;background:#f0f0f0;border-radius:4px;font-size:11px">📞</a>` : ''}}
            </div>`).join('')}}
        </div>` : '';

      const recentEmailsHtml = (ctx.recent_emails || []).length ? `
        <div class="section">
          <div class="label">Recent sequence steps</div>
          ${{ctx.recent_emails.map(em => {{
            const when = em.sent_at ? fmtRel(em.sent_at) : 'queued';
            const flags = [];
            if (em.bounced_at) flags.push(`<span style="color:#c62828">bounced</span>`);
            if (em.complained_at) flags.push(`<span style="color:#c62828">spam</span>`);
            if (em.open_count) flags.push(`<span style="color:#1565c0">${{em.open_count}}× open</span>`);
            if (!em.is_sent) flags.push(`<span style="color:#888">queued</span>`);
            return `
              <div class="activity">
                <div><span class="activity-type">${{escapeHtml(em.step_type || 'email')}}</span><span class="activity-when">· ${{escapeHtml(when)}}</span>${{flags.length ? ` · ${{flags.join(' · ')}}` : ''}}</div>
                <div style="color:#444;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{escapeHtml(em.subject || '')}}</div>
              </div>`;
          }}).join('')}}
        </div>` : '';

      const noteTypes = new Set(['note', 'call', 'call_logged', 'meeting']);
      const otherActs = (ctx.activities || []).filter(a => !noteTypes.has(a.type));
      const activityHtml = otherActs.length ? `
        <div class="section">
          <div class="label">Activity timeline</div>
          ${{otherActs.map(a => `
            <div class="activity">
              <span class="activity-type">${{escapeHtml(a.type)}}</span>
              <span class="activity-when">· ${{escapeHtml(fmtRel(a.at))}}</span>
              <div style="color:#555">${{escapeHtml(a.content)}}</div>
            </div>`).join('')}}
        </div>` : '';

      const imessageFormHtml = c.phone ? `
        <div id="imessage-form" class="section" style="display:none;background:#eaf4ff;border:1px solid #b9dcff;border-radius:8px">
          <div class="label" style="color:#0a558c">💬 Send iMessage to ${{escapeHtml(c.phone)}}</div>
          <textarea id="new-imessage-body" placeholder="Type your message…" rows="3"
            style="width:100%;border:1px solid #ddd;border-radius:6px;padding:6px 8px;font-size:13px;font-family:inherit;resize:vertical;box-sizing:border-box;background:white;color:#1a1a1a"></textarea>
          <div style="font-size:11px;color:#666;margin-top:6px">Sent through your iMessage gateway. Logs to the activity timeline.</div>
          <div style="display:flex;gap:6px;margin-top:10px">
            <button class="btn" onclick="submitIMessage()" style="flex:1">Send iMessage</button>
            <button class="btn secondary" onclick="document.getElementById('imessage-form').style.display='none'">Cancel</button>
          </div>
        </div>` : '';

      const addTaskFormHtml = `
        <div id="add-task-form" class="section" style="display:none;background:#fff8f0;border:1px solid #ffd9b3;border-radius:8px">
          <div class="label">📋 Schedule a task</div>
          <textarea id="new-task-desc" placeholder="What needs to be done?" rows="2"
            style="width:100%;border:1px solid #ddd;border-radius:6px;padding:6px 8px;font-size:12px;font-family:inherit;resize:vertical;box-sizing:border-box;background:white;color:#1a1a1a"></textarea>
          <div style="font-size:11px;color:#666;margin-top:8px;margin-bottom:4px">Due</div>
          <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px">
            <button class="due-btn" data-days="0" onclick="setDueDays(0)">Today</button>
            <button class="due-btn active" data-days="1" onclick="setDueDays(1)">Tomorrow</button>
            <button class="due-btn" data-days="3" onclick="setDueDays(3)">3 days</button>
            <button class="due-btn" data-days="7" onclick="setDueDays(7)">Next week</button>
            <button class="due-btn" data-days="14" onclick="setDueDays(14)">2 weeks</button>
            <button class="due-btn" data-days="30" onclick="setDueDays(30)">30 days</button>
            <button class="due-btn" data-days="-1" onclick="setDueDays(-1)" style="grid-column:span 2">No due date</button>
          </div>
          <div style="display:flex;gap:6px;margin-top:10px">
            <button class="btn" onclick="submitAddTask()" style="flex:1">Create task</button>
            <button class="btn secondary" onclick="document.getElementById('add-task-form').style.display='none'">Cancel</button>
          </div>
        </div>`;

      const appCompanyUrl = APP_URL + '/?company_id=' + co.id;

      root.innerHTML = `
        <div class="section">
          <div class="row">
            <div style="flex:1;min-width:0">
              <h2 style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{escapeHtml(co.name || 'Unknown company')}}</h2>
              <div style="color:#666;margin-top:2px;font-size:12px">${{escapeHtml(c.full_name || c.email || '')}}${{c.title ? ' · ' + escapeHtml(c.title) : ''}}</div>
            </div>
            <a href="${{appCompanyUrl}}" target="_blank" style="font-size:18px;text-decoration:none">↗</a>
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
            style="width:100%;border:1px solid #ddd;border-radius:6px;padding:6px 8px;font-size:12px;font-family:inherit;resize:vertical;box-sizing:border-box;background:white;color:#1a1a1a"></textarea>
          <div style="display:flex;gap:6px;margin-top:6px">
            <button class="btn secondary" onclick="saveInlineNote('note')" style="flex:1">Save note</button>
            <button class="btn secondary" onclick="saveInlineNote('call_logged')" style="flex:1">Save as call</button>
          </div>
        </div>

        ${{imessageFormHtml}}
        ${{addTaskFormHtml}}
        ${{notesHtml}}
        ${{tasksHtml}}
        ${{seqHtml}}
        ${{auditHtml}}
        ${{otherContactsHtml}}
        ${{recentEmailsHtml}}
        ${{activityHtml}}
      `;

      const ta = document.getElementById('quick-note');
      if (ta) ta.addEventListener('keydown', (e) => {{
        if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {{ e.preventDefault(); saveInlineNote('note'); }}
      }});
    }}

    // ---------- Action handlers ----------
    function callNow(phone, contactId) {{
      window.open(_deepLinkUrl('/?dial=' + encodeURIComponent(phone || '') + '&contact_id=' + (contactId || '')), 'prospector-dialer');
    }}

    function scheduleMeeting() {{
      const c = _currentContext && _currentContext.contact;
      if (!c) return;
      window.open(_deepLinkUrl('/?schedule=1&contact_id=' + c.id), 'prospector-schedule');
    }}

    async function copyEmail(addr) {{
      try {{ await navigator.clipboard.writeText(addr); toast('Copied: ' + addr, 'success'); }}
      catch (e) {{ toast('Could not copy', 'error'); }}
    }}

    function quickIMessage() {{
      const form = document.getElementById('imessage-form');
      if (!form) return;
      const willShow = form.style.display === 'none';
      form.style.display = willShow ? 'block' : 'none';
      if (willShow) {{ const ta = document.getElementById('new-imessage-body'); if (ta) setTimeout(() => ta.focus(), 30); }}
    }}

    async function submitIMessage() {{
      const c = _currentContext && _currentContext.contact;
      if (!c) return;
      const ta = document.getElementById('new-imessage-body');
      const body = ((ta && ta.value) || '').trim();
      if (!body) {{ toast('Type a message first', 'error'); return; }}
      const r = await authFetch('/api/integrations/sidebar/send-imessage', {{
        method: 'POST', body: JSON.stringify({{ contact_id: c.id, body }})
      }});
      if (r && r.ok) {{
        toast('iMessage sent', 'success');
        document.getElementById('imessage-form').style.display = 'none';
        loadContext();
      }} else if (r) {{
        toast('Not sent: ' + (r.reason || 'unknown error'), 'error');
      }}
    }}

    function toggleAddTask() {{
      const form = document.getElementById('add-task-form');
      if (!form) return;
      const willShow = form.style.display === 'none';
      form.style.display = willShow ? 'block' : 'none';
      if (willShow) {{
        _newTaskDueDays = 1;
        const ta = document.getElementById('new-task-desc');
        if (ta) {{ ta.value = ''; setTimeout(() => ta.focus(), 30); }}
        document.querySelectorAll('.due-btn').forEach(b => b.classList.toggle('active', parseInt(b.getAttribute('data-days'), 10) === 1));
      }}
    }}

    function setDueDays(days) {{
      _newTaskDueDays = days;
      document.querySelectorAll('.due-btn').forEach(b => b.classList.toggle('active', parseInt(b.getAttribute('data-days'), 10) === days));
    }}

    async function submitAddTask() {{
      const c = _currentContext && _currentContext.contact;
      const co = _currentContext && _currentContext.company;
      if (!co) return;
      const ta = document.getElementById('new-task-desc');
      const desc = ((ta && ta.value) || '').trim();
      if (!desc) {{ toast('Type a description first', 'error'); return; }}
      const r = await authFetch('/api/integrations/sidebar/create-task', {{
        method: 'POST',
        body: JSON.stringify({{ company_id: co.id, contact_id: c ? c.id : null, description: desc, due_in_days: _newTaskDueDays >= 0 ? _newTaskDueDays : null }})
      }});
      if (r && r.ok) {{
        toast('Task created', 'success');
        document.getElementById('add-task-form').style.display = 'none';
        loadContext();
      }}
    }}

    async function saveInlineNote(kind) {{
      const ta = document.getElementById('quick-note');
      if (!ta) return;
      const text = (ta.value || '').trim();
      if (!text) {{ toast('Type a note first', 'error'); return; }}
      const c = _currentContext && _currentContext.contact;
      const co = _currentContext && _currentContext.company;
      if (!c || !co) return;
      ta.disabled = true;
      const r = await authFetch('/api/integrations/sidebar/log-activity', {{
        method: 'POST',
        body: JSON.stringify({{ contact_id: c.id, company_id: co.id, text, activity_type: kind }})
      }});
      ta.disabled = false;
      if (r && r.id) {{
        toast(kind === 'call_logged' ? 'Call logged' : 'Note saved', 'success');
        ta.value = '';
        loadContext();
      }}
    }}

    async function setStatus(newStatus) {{
      const c = _currentContext && _currentContext.contact;
      const co = _currentContext && _currentContext.company;
      if (!co) return;
      const r = await authFetch('/api/integrations/sidebar/set-status', {{
        method: 'POST',
        body: JSON.stringify({{ company_id: co.id, contact_id: c ? c.id : null, new_status: newStatus }})
      }});
      if (r && r.ok) {{
        toast('Status → ' + r.status, 'success');
        loadContext();
      }}
    }}

    async function completeTask(taskId) {{
      const r = await authFetch('/api/integrations/sidebar/complete-task', {{
        method: 'POST', body: JSON.stringify({{ task_id: taskId }})
      }});
      if (r && r.ok) loadContext();
    }}

    // ---------- Kick off ----------
    if (_jwt) loadContext(); else renderLoginGate();
  </script>
</body>
</html>"""
