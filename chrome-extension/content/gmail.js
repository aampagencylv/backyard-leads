// Gmail content script — v2.
//
// Goals over v1:
//   1. Don't show the panel on Gmail's inbox-list view. Only show when
//      the user has a thread open OR a compose window focused.
//   2. Don't overlap Gmail content. When the panel is expanded, push
//      Gmail's main pane to the left so they sit side-by-side.
//   3. Pick the PROSPECT email by parsing the latest expanded message's
//      `From` header — not "any email in the DOM" (which v1 occasionally
//      mis-picked when an old message in the thread had a different
//      sender).
//   4. Collapsed state keeps a thin reveal tab on the right edge so the
//      user can pop the panel back at any time.
//   5. Detect compose recipients — when composing, the To field's
//      recipient becomes the probed contact.
//
// What it does NOT do:
//   - Inject into Gmail's native right rail (would require Google's
//     Workspace Add-on framework). We use a clean fixed-position panel
//     instead, but adjust Gmail's layout so they coexist.

const APP_URL = "https://prospector.backyardmarketingpros.com";
const SIDEBAR_WIDTH = 380;
const REVEAL_TAB_WIDTH = 18;
const STORAGE_KEY_COLLAPSED = "prospector_gmail_collapsed";

let _hostEl = null;
let _iframeEl = null;
let _lastProbe = "";    // last email we asked the iframe to load
let _jwt = "";
let _myEmail = "";

// ----------------------------------------------------------------------
// Boot
// ----------------------------------------------------------------------

(async function init() {
  _jwt = await fetchToken();
  ensureHost();
  // Read once
  refreshProbe();
  // Gmail SPA navigation: hashchange covers most thread switches
  window.addEventListener("hashchange", () => setTimeout(refreshProbe, 200));
  // DOM mutations cover compose-window open, infinite-scroll, etc.
  // Throttled to 500ms to avoid CPU churn.
  const obs = new MutationObserver(throttle(refreshProbe, 500));
  obs.observe(document.body, { childList: true, subtree: true });
})();

async function fetchToken() {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage({ type: "get_auth" }, (r) => resolve((r && r.token) || ""));
    } catch (e) { resolve(""); }
  });
}

// Forward iframe-originated messages (auth expiry signals from the
// embed sidebar) into the background service worker. The iframe is at
// prospector.bymp.com; its window.parent is THIS content script's
// window, so a postMessage from inside the iframe fires here.
window.addEventListener("message", (ev) => {
  const data = ev && ev.data;
  if (!data || typeof data !== "object") return;
  // Only trust messages whose origin is our app
  if (ev.origin !== APP_URL) return;
  if (data.type === "auth_expired") {
    try { chrome.runtime.sendMessage({ type: "auth_expired" }); } catch (e) {}
  }
});

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "token_updated") {
    _jwt = msg.token || "";
    postToIframe({ type: "set_token", token: _jwt });
    // Re-probe so a fresh login immediately renders context
    refreshProbe(true);
  } else if (msg.type === "toggle_sidebar") {
    setCollapsed(_hostEl && _hostEl.dataset.collapsed !== "1" ? true : false);
  }
});

// ----------------------------------------------------------------------
// "What's the current prospect?" detection
// ----------------------------------------------------------------------

function getMyGmailAddress() {
  if (_myEmail) return _myEmail;
  // Multiple stable hooks across Gmail variants
  const el = document.querySelector('[data-email]');
  if (el && el.getAttribute("data-email")) {
    _myEmail = el.getAttribute("data-email").trim().toLowerCase();
    return _myEmail;
  }
  const meta = document.querySelector('meta[name="user-email"]');
  if (meta && meta.content) {
    _myEmail = meta.content.trim().toLowerCase();
    return _myEmail;
  }
  // Last resort: parse from .gb_A title (account avatar tooltip)
  const av = document.querySelector('.gb_A[aria-label]');
  if (av) {
    const m = (av.getAttribute("aria-label") || "").match(/[\w.+-]+@[\w-]+\.[\w.-]+/);
    if (m) {
      _myEmail = m[0].toLowerCase();
      return _myEmail;
    }
  }
  return "";
}

function isThreadOpen() {
  // Gmail hash patterns for "deep" views (specific thread open):
  //   #inbox/<thread_id>
  //   #label/<label>/<thread_id>
  //   #search/<q>/<thread_id>
  //   #sent/<thread_id>
  //   #all/<thread_id>
  // Plus compose windows: #inbox?compose=new
  const hash = (window.location.hash || "").replace(/^#/, "");
  if (/^(inbox|sent|label|search|all|starred|drafts|snoozed|spam|trash|imp)\/.+\/[A-Za-z0-9]+/.test(hash)) {
    return true;
  }
  // Detect open compose windows even when the URL doesn't change
  if (document.querySelector('.AD .nH .aDh, .M9, [role="dialog"][aria-label*="compose" i]')) {
    return true;
  }
  return false;
}

function getFocusedMessageEmail() {
  // Strategy: find the LATEST visible message in the open thread. Gmail
  // expands one message by default (the most recent unread, or the
  // bottom one if everything's read). We look for the bottom-most
  // message wrapper (`.adn`) and read the email from its expanded
  // header (`.gE [email]`).
  const me = getMyGmailAddress();
  const messages = Array.from(document.querySelectorAll(".adn"));
  if (messages.length > 0) {
    // Walk from the bottom up — most recently sent is at the end.
    for (let i = messages.length - 1; i >= 0; i--) {
      const wrap = messages[i];
      // Skip collapsed messages (no header rendering)
      const headerEmail = wrap.querySelector(".gE [email], .iv [email]");
      const v = headerEmail && headerEmail.getAttribute("email");
      if (v) {
        const lower = v.trim().toLowerCase();
        // If the latest sender is US, fall back to the To: address
        // (e.g. the BDR just sent — they want the prospect's context)
        if (lower && lower !== me) return lower;
        // Try the to_fields of this same message
        const toEmail = wrap.querySelector(".cf .g2[email], .iw .g2[email]");
        if (toEmail) {
          const tv = (toEmail.getAttribute("email") || "").trim().toLowerCase();
          if (tv && tv !== me) return tv;
        }
      }
    }
  }

  // Compose window — read the To: chip
  const composeRecipient = document.querySelector('div[role="dialog"] .vR .vT[email], .M9 .vT[email]');
  if (composeRecipient) {
    const v = (composeRecipient.getAttribute("email") || "").trim().toLowerCase();
    if (v && v !== me) return v;
  }

  // Generic fallback: any [email=…] that isn't us
  const all = document.querySelectorAll('[email]');
  for (const el of all) {
    const v = (el.getAttribute("email") || "").trim().toLowerCase();
    if (v && v !== me && v.includes("@")) return v;
  }
  return "";
}

// ----------------------------------------------------------------------
// Iframe host management
// ----------------------------------------------------------------------

function ensureHost() {
  if (_hostEl && document.body.contains(_hostEl)) return;

  const host = document.createElement("div");
  host.id = "prospector-sidebar-host";
  host.style.cssText = `
    position: fixed;
    top: 64px;
    right: 0;
    width: ${SIDEBAR_WIDTH}px;
    height: calc(100vh - 64px);
    background: white;
    border-left: 1px solid #e3e3e3;
    box-shadow: -2px 0 10px rgba(0,0,0,0.08);
    z-index: 2147483600;
    transform: translateX(${SIDEBAR_WIDTH}px);
    transition: transform 0.22s cubic-bezier(.4,0,.2,1);
    display: none;
  `;
  host.dataset.collapsed = "1";  // start collapsed; show only when a probe lands

  // Header bar
  const header = document.createElement("div");
  header.style.cssText = `
    display:flex;align-items:center;gap:8px;padding:8px 12px;
    background:#1B5E20;color:white;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    font-size:13px;font-weight:600;`;
  header.innerHTML = `
    <span>🌳 Prospector</span>
    <span style="flex:1"></span>
    <button id="prospector-collapse" title="Collapse" style="background:transparent;border:0;color:white;cursor:pointer;font-size:16px;line-height:1;padding:2px 6px">›</button>`;

  // Iframe
  const iframe = document.createElement("iframe");
  iframe.id = "prospector-sidebar-iframe";
  iframe.style.cssText = `width:100%;height:calc(100% - 36px);border:0;display:block;background:white;`;
  iframe.src = buildIframeSrc({});

  host.appendChild(header);
  host.appendChild(iframe);
  document.body.appendChild(host);
  _hostEl = host;
  _iframeEl = iframe;

  document.getElementById("prospector-collapse").addEventListener("click", () => setCollapsed(true));

  // Reveal tab — a slim sliver on the right edge that brings the panel
  // back. Always present once the host has been mounted.
  ensureRevealTab();

  // Restore previous collapsed preference (if user collapsed before, stay collapsed)
  chrome.storage.local.get([STORAGE_KEY_COLLAPSED], (data) => {
    const wasCollapsed = !!(data && data[STORAGE_KEY_COLLAPSED]);
    if (wasCollapsed) setCollapsed(true);
  });
}

function ensureRevealTab() {
  if (document.getElementById("prospector-reveal-tab")) return;
  const tab = document.createElement("div");
  tab.id = "prospector-reveal-tab";
  tab.title = "Open Prospector sidebar";
  tab.style.cssText = `
    position: fixed; top: 50%; right: 0; transform: translateY(-50%);
    width: ${REVEAL_TAB_WIDTH}px; min-height: 80px;
    background: #1B5E20; color: white;
    border-radius: 6px 0 0 6px;
    cursor: pointer; z-index: 2147483599;
    display: none;
    align-items: center; justify-content: center;
    font-size: 11px; font-weight: 700; letter-spacing: 1px;
    box-shadow: -2px 0 8px rgba(0,0,0,0.18);
    writing-mode: vertical-rl; text-orientation: mixed;
    padding: 8px 0;
    transition: width 0.12s;
  `;
  tab.textContent = "PROSPECTOR";
  tab.addEventListener("mouseenter", () => { tab.style.width = (REVEAL_TAB_WIDTH + 4) + "px"; });
  tab.addEventListener("mouseleave", () => { tab.style.width = REVEAL_TAB_WIDTH + "px"; });
  tab.addEventListener("click", () => setCollapsed(false));
  document.body.appendChild(tab);
}

function setHostVisible(visible) {
  if (!_hostEl) return;
  if (visible) {
    _hostEl.style.display = "block";
    // Push Gmail's content left so we don't overlap
    if (_hostEl.dataset.collapsed !== "1") {
      document.body.style.marginRight = SIDEBAR_WIDTH + "px";
    }
  } else {
    _hostEl.style.display = "none";
    document.body.style.marginRight = "";
    const tab = document.getElementById("prospector-reveal-tab");
    if (tab) tab.style.display = "none";
  }
}

function setCollapsed(collapsed) {
  if (!_hostEl) return;
  if (collapsed) {
    _hostEl.dataset.collapsed = "1";
    _hostEl.style.transform = `translateX(${SIDEBAR_WIDTH}px)`;
    document.body.style.marginRight = "";
    const tab = document.getElementById("prospector-reveal-tab");
    if (tab && _hostEl.style.display !== "none") tab.style.display = "flex";
  } else {
    _hostEl.dataset.collapsed = "0";
    _hostEl.style.transform = "translateX(0)";
    document.body.style.marginRight = SIDEBAR_WIDTH + "px";
    const tab = document.getElementById("prospector-reveal-tab");
    if (tab) tab.style.display = "none";
  }
  chrome.storage.local.set({ [STORAGE_KEY_COLLAPSED]: collapsed });
}

function buildIframeSrc(opts) {
  const parts = [];
  if (_jwt) parts.push("t=" + encodeURIComponent(_jwt));
  if (opts.email) parts.push("email=" + encodeURIComponent(opts.email));
  return APP_URL + "/integrations/embed/sidebar" + (parts.length ? "?" + parts.join("&") : "");
}

function postToIframe(msg) {
  if (!_iframeEl || !_iframeEl.contentWindow) return;
  try { _iframeEl.contentWindow.postMessage(msg, APP_URL); } catch (e) {}
}

// ----------------------------------------------------------------------
// Probe loop — decide if/what to show
// ----------------------------------------------------------------------

function refreshProbe(force) {
  ensureHost();

  if (!isThreadOpen()) {
    setHostVisible(false);
    _lastProbe = "";
    return;
  }
  const email = getFocusedMessageEmail();
  if (!email) {
    // Thread open but we can't tell who — keep panel hidden (don't
    // distract the user with a blank panel)
    setHostVisible(false);
    _lastProbe = "";
    return;
  }
  setHostVisible(true);
  if (email === _lastProbe && !force) return;
  _lastProbe = email;
  if (!_iframeEl.dataset.booted) {
    _iframeEl.src = buildIframeSrc({ email });
    _iframeEl.dataset.booted = "1";
  } else {
    postToIframe({ type: "set_email", email });
  }
}

// ----------------------------------------------------------------------
// Util
// ----------------------------------------------------------------------

function throttle(fn, ms) {
  let scheduled = false;
  return function () {
    if (scheduled) return;
    scheduled = true;
    setTimeout(() => { scheduled = false; fn(); }, ms);
  };
}
