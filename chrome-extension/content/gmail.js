// Gmail content script for the Prospector CRM extension.
//
// What it does:
//   1. Watches for "thread is open" state inside Gmail's SPA. Gmail
//      rewrites its URL hash on every thread navigation, so we use
//      hashchange + a mutation observer as redundant signals.
//   2. Extracts the prospect's email from the open thread DOM.
//   3. Injects a fixed-position sidebar iframe along the right side
//      of the Gmail window, pointed at our embedded sidebar endpoint.
//   4. PostMessages the iframe with the new email each time a
//      different thread is opened — the iframe refetches context
//      without reloading.
//   5. Listens for {type: "token_updated"} from background and
//      forwards to the iframe so a fresh login propagates instantly.

const APP_URL = "https://prospector.backyardmarketingpros.com";
const SIDEBAR_WIDTH = 380;

let _sidebarEl = null;
let _lastEmail = "";
let _jwt = "";

// ----------------------------------------------------------------------
// Bootstrap
// ----------------------------------------------------------------------

(async function init() {
  _jwt = await fetchToken();
  injectSidebar();
  pollForThread();   // immediate first read
  // Re-poll on hash change (Gmail navigation) + on DOM mutations
  // (compose window open, thread switch within label, etc).
  window.addEventListener("hashchange", () => setTimeout(pollForThread, 200));
  const obs = new MutationObserver(throttle(() => pollForThread(), 500));
  obs.observe(document.body, { childList: true, subtree: true });
})();

async function fetchToken() {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage({ type: "get_auth" }, (r) => resolve((r && r.token) || ""));
    } catch (e) { resolve(""); }
  });
}

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "token_updated") {
    _jwt = msg.token || "";
    // Push to iframe — it'll re-render
    postToIframe({ type: "set_token", token: _jwt });
    if (_lastEmail) {
      // Resync the context now that we have a fresh token
      postToIframe({ type: "set_email", email: _lastEmail });
    }
  } else if (msg.type === "toggle_sidebar") {
    toggleSidebar();
  }
});

// ----------------------------------------------------------------------
// DOM scraping — find the prospect's email on the open thread.
//
// Gmail uses several stable hooks across A/B test variants:
//   - elements with attribute `email="..."` (the most reliable;
//     present on every "from" / "to" header card)
//   - `[data-hovercard-id="..."]` (hovercard pre-loads, holds the
//     identifier — usually an email address)
//   - `mailto:` href values on the header.
// We collect candidates in priority order and pick the first that:
//   (a) is not the user's own Gmail address, and
//   (b) is not in the team-emails list returned by our backend
//   (handled server-side; we just send the first non-self email).
// ----------------------------------------------------------------------

function getMyGmailAddress() {
  // Gmail puts the signed-in user's email at the top-right account
  // chooser. The data attribute `data-email` is stable; fall back to
  // an aria-label parse if Google A/B-tests it out.
  const el = document.querySelector('[data-email]');
  if (el && el.getAttribute("data-email")) return el.getAttribute("data-email").trim().toLowerCase();
  const meta = document.querySelector('meta[name="user-email"]');
  if (meta && meta.content) return meta.content.trim().toLowerCase();
  return "";
}

function getOpenThreadEmail() {
  // Only run when a thread is open — Gmail puts a `#inbox/<id>` or
  // `#label/<x>/<id>` in the URL hash; otherwise we're in a list view.
  const hash = window.location.hash || "";
  if (!/^#?(?:inbox|sent|label|search|all|starred|drafts|snoozed|spam|trash|imp)\/.+\/[A-Za-z0-9]+/.test(hash.replace(/^#/, ""))) {
    // Not deep enough — list view or filter, no specific thread
    return "";
  }
  const me = getMyGmailAddress();
  const candidates = [];

  // Primary: any [email=...] attribute under the open thread container
  // ".adn" wraps each visible message; ".ii" wraps the body. The
  // headers above each have spans with email="..." attributes.
  document.querySelectorAll('.adn [email], .gE [email], .iv [email]').forEach((el) => {
    const v = (el.getAttribute("email") || "").trim().toLowerCase();
    if (v) candidates.push(v);
  });
  // Fallback: any [data-hovercard-id] that looks like an email
  document.querySelectorAll('[data-hovercard-id]').forEach((el) => {
    const v = (el.getAttribute("data-hovercard-id") || "").trim().toLowerCase();
    if (v.includes("@")) candidates.push(v);
  });
  // Fallback: any mailto: href
  document.querySelectorAll('a[href^="mailto:"]').forEach((el) => {
    const href = el.getAttribute("href") || "";
    const v = href.replace(/^mailto:/i, "").split("?")[0].trim().toLowerCase();
    if (v.includes("@")) candidates.push(v);
  });

  // Dedupe + pick first non-self
  const seen = new Set();
  for (const e of candidates) {
    if (seen.has(e)) continue;
    seen.add(e);
    if (e && e !== me) return e;
  }
  return "";
}

// ----------------------------------------------------------------------
// Sidebar injection
// ----------------------------------------------------------------------

function injectSidebar() {
  if (_sidebarEl && document.body.contains(_sidebarEl)) return;

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
    z-index: 999999;
    transform: translateX(0);
    transition: transform 0.2s ease;
  `;

  // Header strip with collapse + sign-in
  const header = document.createElement("div");
  header.style.cssText = `
    display:flex;align-items:center;gap:8px;padding:8px 12px;
    background:#1B5E20;color:white;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    font-size:13px;font-weight:600;`;
  header.innerHTML = `
    <span>🌳 Prospector</span>
    <span style="flex:1"></span>
    <button id="prospector-sidebar-toggle" title="Hide" style="background:transparent;border:0;color:white;cursor:pointer;font-size:16px;line-height:1">✕</button>`;

  const iframe = document.createElement("iframe");
  iframe.id = "prospector-sidebar-iframe";
  iframe.style.cssText = `width:100%;height:calc(100% - 36px);border:0;display:block;background:white;`;
  iframe.src = buildIframeSrc("");

  host.appendChild(header);
  host.appendChild(iframe);
  document.body.appendChild(host);
  _sidebarEl = host;

  document.getElementById("prospector-sidebar-toggle").addEventListener("click", () => {
    toggleSidebar();
  });
}

function toggleSidebar() {
  if (!_sidebarEl) return;
  const collapsed = _sidebarEl.dataset.collapsed === "1";
  if (collapsed) {
    _sidebarEl.style.transform = "translateX(0)";
    _sidebarEl.dataset.collapsed = "0";
  } else {
    _sidebarEl.style.transform = `translateX(${SIDEBAR_WIDTH - 24}px)`;
    _sidebarEl.dataset.collapsed = "1";
  }
}

function buildIframeSrc(email) {
  let q = "";
  if (_jwt) q += "t=" + encodeURIComponent(_jwt);
  if (email) q += (q ? "&" : "") + "email=" + encodeURIComponent(email);
  return APP_URL + "/integrations/embed/sidebar" + (q ? "?" + q : "");
}

function postToIframe(msg) {
  const ifr = document.getElementById("prospector-sidebar-iframe");
  if (!ifr || !ifr.contentWindow) return;
  try { ifr.contentWindow.postMessage(msg, APP_URL); } catch (e) {}
}

// ----------------------------------------------------------------------
// Poll-based thread detector
// ----------------------------------------------------------------------

function pollForThread() {
  const email = getOpenThreadEmail();
  if (!email || email === _lastEmail) return;
  _lastEmail = email;
  // If iframe isn't booted yet, set src; otherwise postMessage.
  const ifr = document.getElementById("prospector-sidebar-iframe");
  if (!ifr) return;
  if (!ifr.dataset.booted) {
    ifr.src = buildIframeSrc(email);
    ifr.dataset.booted = "1";
  } else {
    postToIframe({ type: "set_email", email });
  }
}

// ----------------------------------------------------------------------
// Util
// ----------------------------------------------------------------------

function throttle(fn, ms) {
  let scheduled = false;
  return function (...args) {
    if (scheduled) return;
    scheduled = true;
    setTimeout(() => { scheduled = false; fn.apply(this, args); }, ms);
  };
}
