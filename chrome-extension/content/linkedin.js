// LinkedIn content script.
//
// What it does:
//   1. Watches LinkedIn for profile-view pages (linkedin.com/in/<slug>/)
//      and Sales Navigator lead pages (/sales/lead/<id>/).
//   2. Sends the current profile URL to our backend via the embed
//      sidebar's ?linkedin=<url> lookup so the BDR can see if this
//      person is already in Prospector.
//   3. Same fixed-position panel + reveal tab as the Gmail script.
//      A user who has both Gmail and LinkedIn open in different tabs
//      gets a coherent CRM panel on each, persisted token via
//      chrome.storage.local.
//
// What it does NOT scrape:
//   - The profile's email (LinkedIn hides emails behind "connect" /
//     subscription tiers). We let the user "Quick add" with their own
//     email entry when the contact isn't in our CRM yet.

const APP_URL = "https://prospector.backyardmarketingpros.com";
const SIDEBAR_WIDTH = 380;
const REVEAL_TAB_WIDTH = 18;
const STORAGE_KEY_COLLAPSED = "prospector_linkedin_collapsed";

let _hostEl = null;
let _iframeEl = null;
let _lastUrl = "";
let _jwt = "";

// ----------------------------------------------------------------------
// Boot
// ----------------------------------------------------------------------

(async function init() {
  _jwt = await fetchToken();
  ensureHost();
  // Initial probe + listen for SPA nav (LinkedIn uses pushState; no
  // hashchange fires, so we poll the URL on a timer + on visibility)
  refreshProbe();
  setInterval(refreshProbe, 1500);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) setTimeout(refreshProbe, 200);
  });
})();

async function fetchToken() {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage({ type: "get_auth" }, (r) => resolve((r && r.token) || ""));
    } catch (e) { resolve(""); }
  });
}

// Forward auth-expiry signals from the embed iframe into the
// background service worker (same pattern as the Gmail script).
window.addEventListener("message", (ev) => {
  const data = ev && ev.data;
  if (!data || typeof data !== "object") return;
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
    refreshProbe(true);
  }
});

// ----------------------------------------------------------------------
// LinkedIn URL detection
// ----------------------------------------------------------------------

function getCurrentLinkedInProfileUrl() {
  const path = window.location.pathname || "";
  // Public profile pages
  const profileMatch = path.match(/^\/in\/[^/]+\/?$/);
  if (profileMatch) {
    return "https://www.linkedin.com" + (path.endsWith("/") ? path : path + "/");
  }
  // Sales Navigator lead pages
  const salesMatch = path.match(/^\/sales\/lead\/[\w-]+/);
  if (salesMatch) {
    return "https://www.linkedin.com" + path.split("?")[0];
  }
  return "";
}

// ----------------------------------------------------------------------
// Iframe host (identical shape to gmail.js — kept separate so each
// tab has its own host element + its own collapse pref)
// ----------------------------------------------------------------------

function ensureHost() {
  if (_hostEl && document.body.contains(_hostEl)) return;

  const host = document.createElement("div");
  host.id = "prospector-sidebar-host";
  host.style.cssText = `
    position: fixed; top: 64px; right: 0;
    width: ${SIDEBAR_WIDTH}px; height: calc(100vh - 64px);
    background: white; border-left: 1px solid #e3e3e3;
    box-shadow: -2px 0 10px rgba(0,0,0,0.08);
    z-index: 2147483600;
    transform: translateX(${SIDEBAR_WIDTH}px);
    transition: transform 0.22s cubic-bezier(.4,0,.2,1);
    display: none;
  `;
  host.dataset.collapsed = "1";

  const header = document.createElement("div");
  header.style.cssText = `
    display:flex;align-items:center;gap:8px;padding:8px 12px;
    background:#1B5E20;color:white;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    font-size:13px;font-weight:600;`;
  header.innerHTML = `
    <span>🌳 Prospector</span>
    <span style="flex:1"></span>
    <button id="prospector-collapse" title="Collapse" style="background:transparent;border:0;color:white;cursor:pointer;font-size:16px;line-height:1;padding:2px 6px">›</button>`;

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
  ensureRevealTab();

  chrome.storage.local.get([STORAGE_KEY_COLLAPSED], (data) => {
    if (data && data[STORAGE_KEY_COLLAPSED]) setCollapsed(true);
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
    display: none; align-items: center; justify-content: center;
    font-size: 11px; font-weight: 700; letter-spacing: 1px;
    box-shadow: -2px 0 8px rgba(0,0,0,0.18);
    writing-mode: vertical-rl; text-orientation: mixed;
    padding: 8px 0;
  `;
  tab.textContent = "PROSPECTOR";
  tab.addEventListener("click", () => setCollapsed(false));
  document.body.appendChild(tab);
}

function setHostVisible(visible) {
  if (!_hostEl) return;
  if (visible) {
    _hostEl.style.display = "block";
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
  if (opts.linkedin) parts.push("linkedin=" + encodeURIComponent(opts.linkedin));
  return APP_URL + "/integrations/embed/sidebar" + (parts.length ? "?" + parts.join("&") : "");
}

function postToIframe(msg) {
  if (!_iframeEl || !_iframeEl.contentWindow) return;
  try { _iframeEl.contentWindow.postMessage(msg, APP_URL); } catch (e) {}
}

// ----------------------------------------------------------------------
// Probe loop
// ----------------------------------------------------------------------

function refreshProbe(force) {
  ensureHost();
  const url = getCurrentLinkedInProfileUrl();
  if (!url) {
    setHostVisible(false);
    _lastUrl = "";
    return;
  }
  setHostVisible(true);
  if (url === _lastUrl && !force) return;
  _lastUrl = url;
  if (!_iframeEl.dataset.booted) {
    _iframeEl.src = buildIframeSrc({ linkedin: url });
    _iframeEl.dataset.booted = "1";
  } else {
    postToIframe({ type: "set_linkedin", linkedin: url });
  }
}
