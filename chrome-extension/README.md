# Prospector CRM — Chrome Extension

Adds the same CRM sidebar that lives in Missive to Gmail (and, soon, LinkedIn / Missive web).
Opens on the right side of the Gmail window when a thread is open, shows the prospect's
Prospector context: contact, company, sequence, audit, recent activity, all the same action
buttons (Call / iMessage / Schedule meeting / Add task / etc).

## Install (side-load for internal team)

1. Open Chrome → `chrome://extensions`
2. Toggle **Developer mode** on (top right)
3. Click **Load unpacked**
4. Pick the `chrome-extension/` folder from this repo
5. The extension icon appears in the toolbar — click it once and sign in with your Prospector credentials

That's it. Open any Gmail thread and the panel shows up on the right.

To collapse the panel temporarily, click the ✕ in the green header bar — it slides off-screen
but stays warm so reopening is instant. Click the extension icon to bring it back.

## Architecture

- **`manifest.json`** — Manifest v3. Permissions: `storage` (for the JWT), `activeTab`. Host
  permissions for our own backend, Gmail, LinkedIn, Missive web (the last two are placeholders
  for v2).
- **`background.js`** — Service worker that holds the BDR's JWT in `chrome.storage.local` and
  bridges messages between the popup and content scripts.
- **`popup.html` / `popup.js`** — Toolbar icon popup. Login form (POSTs to `/api/auth/login`)
  and signed-in status. Tokens persist across browser restarts.
- **`content/gmail.js`** — Runs on `mail.google.com`. Watches for thread changes via
  `hashchange` + `MutationObserver`, scrapes the prospect's email from the DOM, injects the
  sidebar iframe with `?email=…&t=<jwt>`, and posts subsequent thread changes to the iframe
  via `postMessage`.

The sidebar HTML itself is served by our backend at `/integrations/embed/sidebar` — same
markup/behavior as the Missive sidebar, just bootstrapped via URL/postMessage instead of
the Missive SDK. So feature parity is automatic.

## Adding more hosts later

To enable LinkedIn / Missive web:
1. Add a new `content/<host>.js` script
2. Update `manifest.json` `content_scripts` to include it
3. The new script just needs to:
   - Identify the "currently focused prospect" by scraping the DOM
   - Inject (or reach into) the same iframe
   - PostMessage updates to it when the focused prospect changes
The iframe itself doesn't change.
