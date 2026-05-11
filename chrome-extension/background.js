// Background service worker for the Prospector CRM extension.
//
// Responsibilities:
//   - Holds the BDR's JWT in chrome.storage.local across browser
//     sessions.
//   - Acts as the postMessage bridge between the popup (where login
//     happens) and the active tab's content script (where the
//     sidebar iframe lives).
//   - Wakes on demand only — no persistent state, MV3 service workers
//     are terminated after ~30s idle.

const APP_URL = "https://prospector.backyardmarketingpros.com";
const STORAGE_KEY = "prospector_jwt";

// ----------------------------------------------------------------------
// Auth helpers
// ----------------------------------------------------------------------

async function getToken() {
  const o = await chrome.storage.local.get([STORAGE_KEY]);
  return (o && o[STORAGE_KEY]) || "";
}

async function setToken(token) {
  await chrome.storage.local.set({ [STORAGE_KEY]: token || "" });
  // Broadcast to every open tab so injected sidebars pick up the new
  // token without a manual refresh. Errors swallowed — tab may have
  // closed, content script may not be active, etc.
  try {
    const tabs = await chrome.tabs.query({});
    for (const tab of tabs) {
      try {
        await chrome.tabs.sendMessage(tab.id, { type: "token_updated", token: token || "" });
      } catch (e) { /* tab has no listener — fine */ }
    }
  } catch (e) { /* ignore */ }
}

async function clearToken() {
  await chrome.storage.local.remove(STORAGE_KEY);
  try {
    const tabs = await chrome.tabs.query({});
    for (const tab of tabs) {
      try { await chrome.tabs.sendMessage(tab.id, { type: "token_updated", token: "" }); } catch (e) {}
    }
  } catch (e) {}
}

// ----------------------------------------------------------------------
// Login — popup posts {type: "login", username, password}; we hit
// /api/auth/login on prospector.bymp.com and stash the resulting JWT.
// ----------------------------------------------------------------------

async function login(username, password) {
  const fd = new URLSearchParams();
  fd.set("username", username);
  fd.set("password", password);
  let r;
  try {
    r = await fetch(APP_URL + "/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: fd.toString(),
    });
  } catch (e) {
    return { ok: false, error: "Network error: " + (e && e.message || e) };
  }
  let data;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok || !data || !data.access_token) {
    return { ok: false, error: (data && data.detail) || ("HTTP " + r.status) };
  }
  await setToken(data.access_token);
  return { ok: true, user_email: data.user_email, user_name: data.user_name };
}

// ----------------------------------------------------------------------
// Message router (popup ↔ background ↔ content script)
// ----------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return false;

  if (msg.type === "get_auth") {
    getToken().then((token) => sendResponse({ token }));
    return true; // async response
  }

  if (msg.type === "login") {
    login(msg.username, msg.password).then(sendResponse);
    return true;
  }

  if (msg.type === "logout") {
    clearToken().then(() => sendResponse({ ok: true }));
    return true;
  }

  if (msg.type === "auth_expired") {
    // Content script's iframe reported a 401 — clear the stored token
    // and bump the toolbar badge so the user sees they need to re-login.
    clearToken().then(() => {
      try {
        chrome.action.setBadgeText({ text: "!" });
        chrome.action.setBadgeBackgroundColor({ color: "#c62828" });
      } catch (e) {}
      sendResponse({ ok: true });
    });
    return true;
  }

  return false;
});

// Clear the badge as soon as the popup successfully logs in
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes[STORAGE_KEY] && changes[STORAGE_KEY].newValue) {
    try { chrome.action.setBadgeText({ text: "" }); } catch (e) {}
  }
});
