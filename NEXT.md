# Next Steps & Punch List

> Living doc. Pick up here at the start of each session. Pull `git pull --ff-only origin main` first.
> Last updated by the agent on 2026-05-05 — overnight defensive pass while the user slept.

## Overnight pass (added after the user said goodnight)
- ✅ Fresh tarball backup at `/root/backups/backyard-leads-20260506-*.tar.gz`
- ✅ Daily backup cron installed (`/usr/local/bin/backup-backyard-leads.sh`, 03:00 UTC, 14-day retention, uses sqlite online .backup)
- ✅ README.md committed — repo overview, architecture, ops notes, "where the bodies are buried"
- ✅ Tasks page filter chips — Today / This Week / Overdue / Team Open beyond just My Open
- ⏸️ Held off on email-validation pre-send and Apollo cleanup — touch the send pipeline; want user awake to test

---

## ✅ Shipped this session

1. **Standardized email signature** — fixed-template (`app/templates/email_signature.html`) rendered from 5 user fields (first_name, last_name, nickname, phone_number, scheduling_url). 480px wide, name+title+logo block, 240w logo, line-wrapping prevention.
2. **CRM rebuild** — split the all-in-one `Lead` model into `Company` → `Contact` (multi per company) → `Deal` (multi per company). Activities, Tasks, GeneratedEmails repointed.
3. **Pipeline kanban** — 6-column drag-and-drop (Prospecting → Qualified → Proposal → Negotiation → Closed Won → Closed Lost).
4. **Pursue auto-creates** — clicking Pursue now auto-creates Contact + Deal + Sequence in one shot, all visible on the kanban before any send.
5. **Tasks page** — My Open / Team Open with inline complete checkboxes.
6. **Real CRM dashboard** (5 zones) — KPI strip · Today's Focus · Hot Leads · Pipeline-by-stage bars · Activity Feed.
7. **Engagement scoring + auto-task** — opens & clicks logged as activities; 3+ opens or any click auto-creates a follow-up task for the deal owner (deduped against tasks made in last 3 days).
8. **Stuck-deal alerts** — deals with `updated_at` >14 days old surface on dashboard.
9. **Modal forms** — replaced `prompt()` chains for Add Contact / Add Deal / Add Note / Add Task. Real fields, validation, Esc/backdrop cancels.
10. **Top global search** — debounced search across companies + contacts (name/email/phone/city), live dropdown.
11. **Companies page filters** — defaults to "active" (excludes 'new' raw scrape results); raw results live under Find Leads or "All (incl. raw)".
12. **Contacts page** — list of every contact across companies, filterable by All / With Email / Missing Email.
13. **Find Leads dropdown** — restored 15 BMP service types (pool builders, landscaping, etc.) + Custom.
14. **Sequence preview bug fix** — old `prompt`-based escaping broke on apostrophes; now uses `escapeHtml()` + DOM lookups.
15. **Sequences expanded by default** on Company Detail (no more click-to-expand).
16. **Apollo + Hunter loosening** — both always run, import everything they find (deduped by email).
17. **Hunter limit fix** — was breaking enrich with `limit=25` (Free plan caps at 10).
18. **Netrows core** — `/v1/email-finder/decision-maker` for verified owner emails. **75% hit rate** on test set against BMP prospects.
19. **Netrows Tier 1** —
    - `/people/reverse-lookup` (auto-fires when adding a contact with email but no name)
    - `/google-maps/reviews` (cached on Company; owner replies highlighted)
    - `/people/posts` (on-demand; shows recent LinkedIn posts on contact card for personalization)
    - `/email-finder/by-linkedin` + `/email-finder/by-name` (via 🔍 Lookup Email button)
20. **Email deliverability fix** — removed visible unsubscribe link from email body. Now only `List-Unsubscribe` HTTP headers (Gmail/Outlook native button at top of email). Footer is just postal address.
21. **Settings → API Keys** — Netrows key can now be set/rotated from the UI (DB-backed, falls back to env). New `runtime_config` table.

### Bug fixes mid-session
- `MissingGreenlet` 500 on Company Detail — replaced lazy `company.tags` access with explicit query.
- Stale browser cache after deploy — added `Cache-Control: no-store` on `/`.
- Broken JS escape on a "contact's" placeholder string — fixed; deploy.sh now JS-parse-checks before pushing.
- Cross-machine deploy script (`scripts/deploy.sh`) so home + office Macs can both deploy.

### Migrations chained on every restart (all idempotent)
1. `migrate_signature_fields.py` — name/title/phone/signature → first_name/last_name/nickname/phone_number/scheduling_url
2. `migrate_leads_to_companies.py` — leads → companies/contacts/deals
3. `migrate_netrows_caches.py` — review + posts cache columns
4. `migrate_runtime_config.py` — runtime_config singleton row

---

## 🟡 Action items for the user (5 min each)

| # | What | Why |
|---|---|---|
| 1 | **Subscribe to Netrows Starter** (€49/mo) | Trial credits exhausted; nothing fires until you upgrade |
| 2 | **Rotate the `pk_live_*` API key** in chat history → paste new one in **Settings → API Keys** | Original key was shared in our conversation |
| 3 | **Set real BMP postal address** in `/opt/backyard-leads/.env` (`BMP_POSTAL_ADDRESS=...`) | CAN-SPAM requirement; placeholder is "Backyard Marketing Pros, Las Vegas, NV" |
| 4 | **Gmail forwarding rule → /api/send/webhook/resend** for auto-pause-on-reply | When a prospect replies, the rest of their sequence auto-pauses |

---

## ✅ Completed this session (2026-05-06)

1. User access levels (admin/sales_rep/read_only) + admin user management
2. Apollo cleanup — removed entirely, Netrows + Hunter is the enrichment chain
3. Email validation pre-send — Hunter /v2/email-verifier, blocks sending to invalid
4. Saved views / filter presets — backend ready, frontend dropdowns pending
5. Start Sequence button on Contacts page
6. AI visibility / GEO checks — llms.txt, FAQ schema, content citability, E-E-A-T
7. Company size enrichment via Netrows /companies/by-domain + /companies/details
8. Review range filters (min + max) on Find Leads page
9. De-prioritized basic SEO checks (SSL, H1, meta → low severity)
10. Personal email tone — first name only, no sign-off, casual
11. Auto Pilot campaigns — full campaign system with cron automation
12. Multi-channel sequences — email + LinkedIn steps, reschedule, add/insert steps
13. Manual company creation + CSV upload with auto-enrich + auto-sequence
14. Admin user invites with welcome email
15. Password reset + change password
16. Company filtering (city search, sort, qualify/unqualify)
17. Three-column company detail (contacts left, sequence center, info right)
18. Tag management (create, add, remove on companies)
19. Delete/regenerate sequence buttons
20. LinkedIn links on contact cards
21. BMP package system (Foundation/Essential/Growth/Scale) with auto-recommendation
22. MRR/ARR forecast with pipeline stage probabilities
23. Company Intel panel (LinkedIn company data, Google rating, enrichment summary)

---

## 🟢 Backlog — ranked by ROI

### 🔥 Inbox capture — TWO-PRONGED APPROACH (locked 2026-05-08)

After discussion, the original Missive Phase 1 webhook plan was REPLACED with
a more reliable, inbox-tool-agnostic design. The new approach combines:

**A. Token-based reply catching** (SHIPPED — code-side; awaits DNS + Resend Inbound configuration)
**B. Missive sidebar app** (Phase 2 — next session) — for active capture when BDR initiates from Missive

The BCC log address pattern was rejected — too noisy (captures internal emails,
vendor threads, support, etc.) without explicit BDR intent.

---

#### A. Token-based reply catching — SHIPPED, AWAITING SETUP

Every outgoing email now has `Reply-To: r-<token>@inbound.bymp.com`. Resend
Inbound catches all mail at that subdomain via catch-all routing and POSTs to
`/api/email/inbound`. The webhook:

1. Extracts the token from the `To`/`Cc` list
2. Looks up the GeneratedEmail row by reply_token
3. Logs an `email_replied` Activity (or `email_auto_response` if it looks like
   an OOO/bounce — heuristic match on From + Subject)
4. Auto-pauses the contact's sequence
5. Bumps company.status → 'replied'
6. Forwards the message to the BDR's actual inbox (`user@bymp.com` → Missive)
   with Reply-To set to the prospect's real email so follow-up replies in this
   thread happen normally inside Missive

Works regardless of inbox tool — the BDR's choice of Missive vs. Gmail vs.
Outlook doesn't matter. Critical for the SaaS plan.

**Setup steps Steve needs to do (~5 min):**

> Update 2026-05-08: The existing `go.backyardmarketingpros.com` domain in
> Resend ALREADY has Receiving enabled (Steve confirmed via screenshot —
> MX → inbound-smtp.us-east-1.amazonaws.com is verified). So there's no
> new DNS or new domain to add. We just configure the webhook destination
> in Resend's dashboard.

1. **In Resend dashboard, configure the inbound webhook:**
   - Look for either: (a) `Webhooks` in the left nav, or (b) `go.backyardmarketingpros.com` → Configuration tab → Inbound section. Resend has rearranged this UI a couple of times.
   - Add a new webhook for the `email.received` event scoped to
     `go.backyardmarketingpros.com`.
   - Endpoint URL: `https://prospector.backyardmarketingpros.com/api/email/inbound`
   - Save the signing secret Resend generates.

2. **Paste the signing secret into the platform `.env`**:
   ```bash
   ssh vps "echo 'RESEND_WEBHOOK_SECRET=<the-secret-from-resend>' >> /opt/backyard-leads/.env && systemctl restart backyard-leads"
   ```
   Without this, the webhook accepts any payload (fine for testing, bad for
   prod — anyone who knew the URL could forge fake replies).

3. **Test it**: send yourself a test sequence email. Reply from a different
   email address. Within ~30 seconds you should see:
   - `email_replied` Activity on the contact's timeline
   - The contact's sequence auto-paused
   - The reply forwarded to your Missive inbox with the prospect's email as
     Reply-To (so when you hit Reply in Missive, it goes to them, not back
     through us)

4. **Watch for**: auto-responders / OOO replies should log as
   `email_auto_response` (different icon, doesn't pause sequence). Tune the
   detection heuristic in `email_inbound_routes._looks_like_auto_response()`
   if you see false positives.

4. **Test it**: send yourself a test sequence email. Reply from a different
   email address. Within ~30 seconds you should see:
   - `email_replied` Activity on the contact's timeline
   - The contact's sequence auto-paused
   - The reply forwarded to your Missive inbox with the prospect's email as
     Reply-To (so when you hit Reply in Missive, it goes to them, not back
     through us)

5. **Watch for**: auto-responders / OOO replies should log as
   `email_auto_response` (different icon, doesn't pause sequence). Tune the
   detection heuristic in `email_inbound_routes._looks_like_auto_response()`
   if you see false positives.

---

#### B. Missive sidebar app — Phase 2 (next session, ~1 week)

For ACTIVE capture when the BDR initiates a conversation from inside Missive
(not a reply to one of our outbound sequences). This is the use case the
original Missive Phase 1 plan tried to solve via webhooks; the sidebar
approach is cleaner because the BDR explicitly clicks "Add to CRM" instead
of automation guessing what's CRM-worthy.

- Hosted at `https://prospector.backyardmarketingpros.com/missive-sidebar`
- Embedded in Missive as an iframe (Missive supports custom sidebar apps via
  their integrations marketplace OR via direct iframe URL config)
- Shows company/contact card matched on the email's From address
- If contact NOT found: shows "Add to CRM" button → creates Contact + Company
  (using our domain-dedupe helper) + logs initial `email_sent` Activity
- If contact FOUND: shows sequence status, deal info, latest activities, with
  buttons: "Add Note" / "Open in CRM" / "Start Sequence" / "Pause Sequence"
- Authentication: shared secret + Missive's iframe-postMessage protocol so
  the sidebar knows which user is viewing it (Missive passes the current
  user's email as a query param)

Missive API docs: missiveapp.com/help/api
Missive integration / sidebar docs: missiveapp.com/help/integrations

---

#### C. (Deferred) Full Missive send integration

Send FROM Missive instead of Resend, with the sequence creating drafts in
Missive that the BDR reviews + sends. Eliminates Resend dependency. Big lift,
significantly Missive-vendor-locked. Park indefinitely; revisit if Resend
becomes a constraint.

### 🔥 Twilio — full HubSpot Calling replacement [IN PROGRESS]

**Locked decisions (2026-05-06):**
- Per-rep numbers (better caller ID, ~$1.15/mo each)
- Admin UI to buy + assign + release numbers
- Inbound voicemail when rep offline → same pipeline as calls
- **Whisper + Claude for transcripts and call takeaways** (4.5× cheaper than
  Twilio Voice Intelligence; we control the prompt so "call takeaways" /
  coaching suggestions are exactly what we want)
- Browser-only dialer in Phase 1; native mobile app deferred
- Power dialer (Phase 5) human-initiated only (TCPA)
- HubSpot stays parallel during ~3-week transition

**Voice Intelligence vs Whisper+Claude rationale:** The only thing VI does
that Whisper can't is real-time live coaching DURING the call (e.g. "ask
a question, you've been monologuing for 90 sec"). For post-call review
with AI takeaways, custom Claude prompts beat VI's pre-built operators
on flexibility AND cost. If real-time live coaching becomes important
later, we can layer VI on top.

**Goal:** retire HubSpot Sales Hub for calling. Team dials from inside our
CRM, every call lands on the contact's timeline with recording, transcript,
and AI summary, and inbound calls route to the right rep automatically.
SMS is folded in last — calls are the primary unlock.

**Why now:** HubSpot Sales Hub Pro is ~$100/seat/mo. Twilio direct is
~$10/seat/mo all-in. At 5 reps that's $450/mo saved AND we keep all the
data inside our own CRM instead of HubSpot's silo.

**Pricing math (Twilio direct):**
- Voice: $0.013/min outbound, $0.0085/min inbound
- Phone numbers: $1.15/mo each (per rep)
- Recordings: $0.0025/min stored
- Transcription: $0.05/min (Twilio Voice Intelligence) OR ~$0.006/min via
  Whisper (post-call upload). Whisper is 10× cheaper but lags real-time.
- For 500 calls/mo @ 3 min avg = ~$25 talk + $5/rep numbers + $4 recording =
  **≈ $40/mo for 5 reps**, vs $500 on HubSpot.

**Architecture:**
```
Browser (BDR)  ←→  Twilio Voice SDK (WebRTC)  ←→  Twilio Voice
                                                       ↓
                                                 Recording + Transcript
                                                       ↓
                                              Webhook → /api/twilio/voice/*
                                                       ↓
                                       Activity row + Recording URL +
                                       AI Summary on contact timeline
```

#### Phase 1 — Foundation (½ day)
- Twilio account + first phone number for testing
- Buy `+1-702-XXX` Vegas-area-code numbers per rep (better connect rate)
- Add to config: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_API_KEY`,
  `TWILIO_API_SECRET`, `TWILIO_TWIML_APP_SID`. Stored in `runtime_config`
  table so the team rotates from Settings UI without SSH.
- Each rep's `User` model gets `twilio_phone_number` (their assigned caller
  ID) + `twilio_identity` (used for SDK auth). Migration:
  `migrate_twilio_fields.py`.

#### Phase 2 — Click-to-call (the core HubSpot replacement, ~2 days)
**Browser dialer:** Twilio Voice JavaScript SDK, no phone hardware needed.
- New `Dialer` modal — appears when BDR clicks any phone number on a
  Contact card or in the Companies list.
- Modal shows: contact photo / name / title / company, recent activity
  (last 3 timeline entries), sequence status, deal stage + value.
- Live controls: Mute · Hold · Hangup · Transfer · Keypad (DTMF).
- During call: textarea for live notes, outcome dropdown
  (connected · voicemail · no answer · wrong number · gatekeeper · declined).
- After call: auto-saves Activity (`activity_type='call'`, content = notes,
  metadata = {duration, outcome, recording_url, direction}).

**Endpoints:**
- `POST /api/twilio/voice/token` → returns ephemeral SDK access token
  scoped to the BDR's Twilio identity (5-min TTL, refreshed on demand).
- `POST /api/twilio/voice/twiml` → TwiML endpoint Twilio hits when SDK
  initiates a call; returns `<Dial callerId="..." record="record-from-answer">`.
- `POST /api/twilio/voice/status` → status callback receiver (ringing,
  in-progress, completed). Logs duration + direction.
- `POST /api/twilio/voice/recording` → recording-complete webhook. Stores
  URL on the Activity, kicks off transcription job.

**Schema additions:**
```sql
ALTER TABLE activities ADD COLUMN twilio_call_sid VARCHAR(50);
ALTER TABLE activities ADD COLUMN call_duration_seconds INTEGER;
ALTER TABLE activities ADD COLUMN call_direction VARCHAR(20);  -- inbound/outbound
ALTER TABLE activities ADD COLUMN call_outcome VARCHAR(40);    -- connected/voicemail/etc
ALTER TABLE activities ADD COLUMN recording_url VARCHAR(500);
ALTER TABLE activities ADD COLUMN transcript TEXT;
ALTER TABLE activities ADD COLUMN call_summary TEXT;            -- AI-generated
```

#### Phase 3 — Recording + transcription + AI summary (1 day)
- Record everything by default (`record="record-from-answer-dual"` for
  separate channels per side — better transcription).
- 2-party consent compliance: TwiML plays a brief disclosure before
  connecting ("This call may be recorded for quality and training")
  — required in Nevada, California, and 11 other states.
- Post-call worker downloads recording, sends to Whisper for transcript
  (cheaper than Twilio Voice Intelligence, 10× difference).
- Anthropic Claude (Sonnet 4) summarizes: outcome, next steps, sentiment,
  any commitments or objections. Saved on the Activity.
- Timeline entry shows: "📞 15 min call with Bret @ Cacti Landscapes
  — connected · scheduled demo for Tue 2pm" with [▶ Play] + [📄 Transcript]
  + [✨ Summary] buttons.

#### Phase 4 — Inbound routing (1 day)
- Each rep's number forwards to their Twilio identity (their browser).
- When offline, Twilio sends to voicemail with custom greeting.
- Voicemail recording → same pipeline as outbound (transcript + summary).
- Inbound call lookup: if `From` number matches a known Contact, the
  Dialer modal pops on the rep's screen WITH the contact's CRM record
  pre-loaded. (HubSpot has this; we should match it.)
- Unknown caller → modal shows the number with "Add as new contact" CTA.

#### Phase 5 — Reporting + power dialer (1-2 days)
- **Calls per rep per day** (chart on dashboard)
- **Connect rate** = connected / dialed (industry benchmark: 5-15% cold)
- **Average talk time**
- **Outcome funnel**: dialed → connected → demo-booked → closed
- **Power dialer mode**: feed a saved view (e.g. "Stale deals · stage=qualified")
  to the dialer. Auto-advances to next contact when call ends; one-click
  log + dial next.

#### Phase 6 — Messaging — PIVOTED to Blooio iMessage [SHIPPED 2026-05-06]
- **Outbound iMessage via Blooio** (`POST /api/blooio/send`). Sends from
  BMP's dedicated 305 number. Falls back to RCS / SMS automatically when
  the recipient isn't on iMessage — no parallel paths to maintain.
- **Why Blooio over Twilio SMS**: 3-4× higher response rates for B2B cold
  outreach in iPhone-heavy markets, and skips the A2P 10DLC compliance
  burden entirely (no brand registration, no campaign approval, no
  unregistered-traffic surcharge).
- **GHL coexistence**: the same Blooio account also runs an unrelated GHL
  integration. Inbound webhook handler filters by "is the From number a
  known BMP Contact?" — unknown senders return 200 OK and are silently
  ignored, so the GHL integration sees its own traffic untouched. The
  webhook self-registration endpoint (`POST /api/blooio/webhook/setup`,
  admin-only) is idempotent and never modifies other webhooks on the
  account.
- **Inbound handling** (matches email-reply behavior):
  - `message.received` → log Activity `imessage_received`, auto-pause the
    contact's email sequence, bump company status to `replied`
  - STOP keyword → set `Contact.do_not_text=True`, log `sms_opt_out`
  - START keyword → restore opt-in, log `sms_opt_in`
  - `message.delivered` / `message.read` / `message.failed` → update the
    matching `imessage_sent` Activity's metadata
- **Twilio SMS code is dormant, not deleted** — `app/services/twilio_sms.py`
  + the `/api/twilio/sms/*` endpoints stay in tree as a future fallback
  channel if we ever need a Blooio-independent path. STOP/START keyword
  helpers live there and are imported by Blooio's inbound handler.
- **Sequence integration**: "Step type: SMS" sequence step now sends via
  Blooio (when wired up by the sequence-engine task — currently still
  email-only).
- TCPA compliance: STOP keyword auto-honored. Send-window enforcement
  (8am-9pm local time) is built but currently bypassed for human-initiated
  sends from the composer; will be re-enabled when sequences trigger
  auto-sends.

#### Compliance / risk
- **2-party consent recording disclosure** (Phase 3). Required in NV, CA,
  FL, IL, MD, MA, MT, NH, PA, WA, CT, DE.
- **DNC list check** before dialing (Twilio has a National DNC API).
- **Call hours** respect (8am-9pm local time of the dialed number; we
  already store contact timezone via Google Maps).
- **TCPA** for any auto-dialer behavior — Phase 5 power dialer must be
  human-initiated, not auto-fire.

#### Migration path off HubSpot
1. Phase 1+2 ship → invite one rep to dual-tool for a week (HubSpot for
   inbound, Twilio for outbound)
2. Phase 4 ships → port HubSpot inbound number to Twilio
3. Phase 5 reporting parity → shut off HubSpot Sales Hub seats
4. Estimated 3-4 weeks total to complete switchover

#### Endpoint summary
```
POST /api/twilio/voice/token            (BDR's browser fetches token)
POST /api/twilio/voice/twiml            (TwiML for outbound dial)
POST /api/twilio/voice/status           (status callbacks)
POST /api/twilio/voice/recording        (recording-complete)
POST /api/twilio/voice/inbound          (inbound call routing)
POST /api/twilio/sms/inbound            (Phase 6 — DORMANT)
GET  /api/blooio/test                   (Phase 6 — connection test)
POST /api/blooio/send                   (Phase 6 — outbound iMessage)
GET  /api/blooio/capability             (Phase 6 — Enterprise plan only)
POST /api/blooio/inbound                (Phase 6 — Blooio webhook receiver)
POST /api/blooio/webhook/setup          (Phase 6 — admin: register webhook)
POST /api/contacts/{id}/call            (initiate from UI)
GET  /api/twilio/numbers                (admin: list available numbers)
POST /api/twilio/numbers/buy            (admin: purchase a number)
PATCH /api/users/{id}/twilio            (admin: assign number to rep)
GET  /api/dashboard/calls               (per-rep daily call stats)
```

### Sequence engine — Call steps + conditional skip logic
**Note from Steve, mid-Twilio build:** once Twilio is fully wired
(through Phase 6 / SMS), come back here and extend the sequence
engine.

**1. Call steps (creates a BDR task, doesn't auto-dial)**
New `type='call'` step. When the sequence reaches it, we create a
Task on the assigned BDR with contact info + a suggested talk-track.
Sequence advances when the BDR completes the call (via dialer Save
& Close OR by manually marking the Task complete).

Default cadence: 2-3 call steps across a 21-day sequence. Rough
draft below — exact spacing to be refined when we sit down to build:
  Day 0:  Email #1   (cold)
  Day 1:  Call #1     (warm follow-up after the email)
  Day 3:  Email #2   (follow-up)
  Day 5:  LinkedIn    (skip if no URL — see #2)
  Day 7:  Call #2
  Day 10: Email #3
  Day 14: Call #3     (final attempt)
  Day 21: Email #4   (breakup)

**2. Conditional step skipping** — general "skip if missing" logic:
  - LinkedIn step       → skip if `contact.linkedin_url` is null
  - SMS step (Phase 6)  → skip if `contact.phone` is null
  - Call step           → skip if `contact.phone` is null
                          OR contact has a do_not_call flag set
  - Email step          → skip if `contact.email` is null
                          OR `contact.unsubscribed_at` is set
                          (already partially handled)

Implementation sketch:
  - `GeneratedEmail` / sequence-step rows gain a `skip_if` column
    (JSON array: `["no_linkedin"]`, `["no_phone"]`, etc.).
  - Sequence executor checks contact state at runtime; if the
    condition matches, skip and log an Activity ("Skipped LinkedIn
    step — no URL on file") instead of failing.
  - Generation-time logic should also decide what to include
    initially. Cleanest: omit the LinkedIn step entirely when there's
    no URL at sequence-creation time, so the rep doesn't see a
    "skipped" entry on every cadence cycle.
  - Existing LinkedIn-step-creation needs updating to apply this.

**3. Multi-channel default template**
Once #1 + #2 are in place, swap the current default sequence for
the multi-channel one above. Keep an email-only "minimal" template
option for low-priority contacts or follow-on outreach.

---

### 🔥 Website Visitor Tracking (email-to-site intelligence)

Track when a prospect clicks through from an email to backyardmarketingpros.com, then track every page they visit. Auto-alert BDRs when a prospect is actively browsing.

**Three components:**

**1. Tracking Links (wrap URLs in outgoing emails):**
- `TrackingLink` model: token, contact_id, email_id, destination_url, clicked_at
- `GET /t/{token}` — public redirect endpoint, logs click, sets cookie, redirects
- Auto-wrap URLs in generated emails during sequence creation
- Signature links (website, Calendly) also get wrapped

**2. JavaScript Snippet (install on backyardmarketingpros.com):**
```html
<script>
(function(){
  var API='https://prospector.backyardmarketingpros.com/api/track';
  var p=new URLSearchParams(location.search);
  var id=p.get('bmp_id');
  if(id) document.cookie='bmp_visitor='+id+';path=/;max-age=31536000;SameSite=Lax';
  var m=document.cookie.match(/bmp_visitor=([^;]+)/);
  if(m) navigator.sendBeacon(API+'/pageview',JSON.stringify({
    visitor_id:m[1], url:location.href, title:document.title, referrer:document.referrer
  }));
})();
</script>
```
- ~15 lines, no dependencies, non-blocking beacon
- Drops first-party cookie on first tracked visit
- Every subsequent page view tracked back to the contact

**3. Hot Lead Detection (server-side):**
- `PageView` model: visitor_token, contact_id, company_id, url, page_title, session_id
- Session grouping: page views within 30 min = one session
- 3+ pages in a session = auto-create "hot lead" task for assigned BDR
- Pricing page visit = highest priority signal
- "Hot Leads" section on dashboard: contacts active on site in last 30 min
- Timeline entries: "Brett Utter visited /pricing at 2:34pm"

**API endpoints:**
- `GET /t/{token}` — tracking link redirect (public)
- `POST /api/track/pageview` — JS beacon receiver (public, CORS)
- `GET /api/track/activity/{company_id}` — site visits for a company
- `GET /api/track/hot-leads` — contacts on site in last 30 min
- `POST /api/track/generate-link` — create tracking link

**Build phases:**
- Phase 1 (half day): Tracking links + click logging + auto-wrap in emails
- Phase 2 (half day): JS snippet + page view tracking + timeline entries
- Phase 3 (1 day): Hot lead detection + auto-tasks + dashboard section

---

### 🔥 iClosed Integration — Gated Competitor Report + Scheduling

**Context:** Competitor report is gated behind scheduling a call. Prospect sees a blurred preview, must book to unlock. Uses iClosed API instead of Calendly.

**API:** `https://developer.iclosed.io/` — Bearer token auth (`iclosed_<key>`)
- `GET /v1/events/timeSlots` — get available slots
- `POST /v1/eventCalls` — book a call
- `POST /v1/contacts` — create/upsert contact
- Webhooks for real-time booking notifications

**Gate page flow (/report/{token}/compare):**
1. Background starts generating competitor report immediately
2. Page shows blurred comparison table preview
3. Below blur: "Schedule a 15-min call to walk through your results"
4. iClosed booking widget embedded (or custom form that calls iClosed API)
5. When prospect submits (name, phone, email, picks time):
   - Creates/upserts contact in iClosed
   - Books the call via `POST /v1/eventCalls`
   - Updates contact in Prospector CRM (phone number, email)
   - Un-gates the report — full comparison displayed
   - BDR gets URGENT notification with phone number + meeting time
   - Activity logged: "Brett booked a call for Tue 2pm to review competitor report"
6. Repeat visits (after booking) show the full report immediately

**Why iClosed over Calendly:** Team already uses it, has API for programmatic booking, can create contacts and log outcomes, webhooks for real-time notification.

---

### 🔥 Conditional Sequence Logic (if/then for channels)

**Problem:** Sequences include LinkedIn and SMS steps, but many contacts don't have LinkedIn URLs or cell phones. Sending to channels we don't have data for is pointless.

**Solution:** Skip conditions already exist on GeneratedEmail model (`skip_if_json`, `auto_execute`). Need to wire them properly:

**Rules:**
- SMS/iMessage step → skip if no phone number (`skip_if: ['no_phone']`)
- SMS/iMessage step → skip if phone_type = 'landline' (`skip_if: ['landline']`)
- LinkedIn step → skip if no linkedin_url (`skip_if: ['no_linkedin']`)
- Email step → skip if no email (`skip_if: ['no_email']`)
- Any step → skip if contact unsubscribed/opted out

**Dynamic channel addition:**
- When a phone number is ADDED to a contact after sequence was created:
  - Check if there are skipped SMS steps → un-skip them (clear `skipped_at`)
  - Or: auto-insert a new SMS step if the sequence didn't have one
- Same for LinkedIn URL — adding it could trigger adding a LinkedIn step

**Implementation:**
- Sequence engine already evaluates `skip_if_json` — just need to populate it during sequence generation
- Add a contact update hook: when phone/LinkedIn is updated, check for skipped steps

---

### Custom Fields for Companies + Contacts

**Company custom fields:**
- Total annual revenue (number)
- Notes (text, unlimited)
- Facebook URL
- Instagram URL
- Twitter/X URL
- Source (how we found them — Google Maps, referral, upload, manual)
- Industry sub-category

**Contact custom fields:**
- Cell phone (separate from office phone)
- Personal email (separate from work email)
- Facebook URL
- Notes
- Preferred contact method (email, phone, text, LinkedIn)
- Best time to call

**Implementation options:**
1. **JSON blob** — `custom_fields_json TEXT` on Company and Contact. Flexible, no migrations needed for new fields. Query with JSON functions.
2. **Dedicated columns** — one migration per field but better indexing/filtering.
3. **EAV table** — `custom_field_values(entity_type, entity_id, field_name, field_value)`. Most flexible but hardest to query.

**Recommendation:** Hybrid — dedicated columns for the most-used fields (revenue, cell phone, social URLs, notes) and a JSON blob for ad-hoc custom fields users create in the UI.

**Netrows enrichment for social URLs:**
- `/companies/details` sometimes returns social links
- Facebook, Instagram, Twitter could auto-populate during enrichment

---

### Automated Competitor Comparison Report
When prospect clicks "See Your Competitive Comparison" in the audit report:
1. System automatically runs audits on the top 3 SERP competitors (we already have them from DataForSEO)
2. Generates a branded comparison report: side-by-side scores (AI Findability, Citability, Local SEO, Domain Authority, Keywords)
3. Shows what competitors do better (FAQ schema, llms.txt, more backlinks, etc.)
4. Hosted at /report/{token}/competitors as a follow-up to the original audit
5. BDR gets notified when it's ready and sends the link
6. Could auto-send via email sequence if configured

### Other high-value items
- [ ] **Dashboard MRR/ARR cards** — wire forecast API to dashboard KPI strip
- [ ] **Saved views UI** — dropdown on Companies + Pipeline pages (API ready)

### Tier 2 Netrows (~3 hr total)
- [ ] `/businesses/search` (Yellow Pages) — alternative SMB owner finder
- [ ] `/yelp/business-details` + `/yelp/business-reviews` — owner replies on Yelp = same value as Google Maps reviews
- [ ] `/similarweb/website-overview` — real traffic data for qualifying (skip companies with <100 visitors/mo)
- [ ] `/technographics/lookup` (BuiltWith) — confirm tech stack, stronger than our DIY website_intel
- [ ] `/indeed/job-search` (by company) — what they're hiring for = budget signal
- [ ] `/companies/by-domain` (LinkedIn) — staff count + founded year as qualifying inputs

### Tier 3 Netrows — Radar (~3 hr)
- [ ] **Radar webhook receiver** at `/api/netrows/radar` (HMAC verified)
- [ ] **UI** to add/remove monitored profiles (LinkedIn or X) per Contact + per Company
- [ ] **Auto-task** when a monitored prospect changes role/company → "Follow up with X — they just became CMO"

### ✅ Shipped 2026-05-08 session
- [x] **Inbox capture Phase A — Token-based reply catching** with Resend Inbound (replaces the original Missive Phase 1 webhook plan; better because inbox-tool-agnostic). Display name on Reply-To. Svix HMAC verification via Resend's signing secret. Body fetched via `/emails/inbound/{id}`. Signature stripped for timeline preview. Full body stashed in metadata + `📄 Full reply` expand button. Signature mining auto-enriches contact phone (mobile preferred) + LinkedIn URL when fields are empty. Settings panel for rotating the webhook secret.
- [x] **Security cleanup batch** — `/t/{token}` cookie Secure+HttpOnly, restricted CORS, rate limit on `/api/track/pageview`, HEAD `/track.js`, notifications scoped to assigned reps.
- [x] **Contacts page** — sort/filter/merge with phone-type, opted-out, hot-lead, city/state, tag filters; bulk delete + bulk merge with admin gating; sticky multi-select action bar.
- [x] **Ad-hoc email composer** — 📧 Email button on contact cards; rich-text contenteditable; sends via Resend with click-tracking.
- [x] **Companies bulk actions** — assign / tag / status / enrich / delete via the existing multi-select bar; admin-gated.
- [x] **Onboarding walkthrough** — 10-step guided product tour with vanilla JS overlay + spotlight; auto-starts on first login; Settings → Restart Tour button.
- [x] **Item 1 dedupe Company by website domain** — `domain` column on Company + canonical `normalize_domain()` helper + dedupe at create + on CSV upload.
- [x] **Lost-reason dropdown** on `closed_lost` deals — 7 canned options + free-text notes; combined into single column.
- [x] **Send caps per sender per day** — 50/day default (env-overrideable); engine defers to next-morning when cap hit.
- [x] **Sequence step card visual identity** — channel-colored left rails (📧 green / 💼 LinkedIn-blue / 📞 orange / 💬 iMessage-blue), numbered circle badge, white card + shadow, DONE pill on sent steps.

### 🔥 NEXT SESSION (locked 2026-05-08 with Steve)
- [ ] **Missive sidebar app (Inbox capture Phase B)** — for ACTIVE capture when BDR initiates a conversation from inside Missive. Hosted at `/missive-sidebar`, embedded as an iframe. Shows company/contact card matched on the email's From address. If contact NOT found: "Add to CRM" button → creates Contact + Company (using domain dedupe) + logs initial Activity. If contact FOUND: shows sequence status, deal info, latest activities, with buttons "Add Note" / "Open in CRM" / "Start Sequence" / "Pause Sequence". Auth via shared secret + Missive's iframe-postMessage protocol.
- [ ] **Google OAuth** — for two reasons:
  - Send email from the user's Gmail account (replaces Resend for those who want native Gmail send)
  - Read calendar availability (handy if iClosed ever needs replacement)
  - Probably wire `Sign in with Google` for user auth too while we're in there

### Security + code-cleanup followups (from end-of-session audit, 2026-05-06)
- [x] Merge Company endpoint admin-gated (was open to all roles — fixed in same session)
- [x] `Set-Cookie` flags on /t/{token} redirect — Secure + HttpOnly (shipped 2026-05-07)
- [x] CORS posture — restricted allow_origins, dropped allow_credentials (shipped 2026-05-07)
- [x] Rate limiting on /api/track/pageview — 60/60s sliding window per IP (shipped 2026-05-07)
- [x] Notifications endpoint scope — sales_rep filtered to assigned companies (shipped 2026-05-07)
- [x] HEAD method on /track.js — returns 200 with same headers as GET (shipped 2026-05-07)
- [ ] **Split company_routes.py** — 1241 lines and growing. Move merge + enrich + pursue + reviews into separate modules under `app/routes/companies/`.
- [ ] **Dormant Twilio SMS code** in `app/services/twilio_sms.py` + `/api/twilio/sms/*` endpoints. Kept on purpose (might re-enable as a fallback channel) but worth re-evaluating in a few months — if we never need it, delete.

### Compliance / hygiene
- [x] Send caps per domain per day (shipped 2026-05-08)
- [x] Lost-reason capture on closed_lost deals (shipped 2026-05-08, dropdown)
- [x] Dedupe Company creation by website domain (shipped 2026-05-08)
- [ ] **Bounce auto-handling** is partially wired; ensure UI shows BOUNCED contacts clearly and prompts for alternate email
- [ ] **Merge UX gap**: Companies list defaults to hiding `status=new` (raw scrape) rows, so a duplicate where one row is in `new` and another is in `sequencing` can't be merged from the UI — checkboxes only render on the Companies list. Either (a) make duplicates always visible in a "Possible duplicates" panel, or (b) add a "Merge into existing company..." action on the company-detail page that lets you pick another company by name/search. Steve hit this with the AAMP duplicate (2026-05-07); manually merged via API call.
- [ ] **Auto-response false-positive tuning** — the inbound webhook detects bounces / OOO replies via heuristic (mailer-daemon@, "out of office", etc.). Watch for false positives once real prospects start replying; tune `_looks_like_auto_response` in `email_inbound_routes.py` if needed.

### UX polish
- [ ] **Mobile PWA** polish — currently desktop-first
- [ ] **Universal Cmd+K search** — power-user efficiency
- [ ] **CSV import** for bulk uploading existing customer data
- [ ] **Bulk actions** — mass tag, mass assign, mass enrich

### Foundation
- [ ] **README** — explain the architecture, deploy flow, how to run locally
- [ ] **Smoke tests** — at least pytest for the migration scripts and a couple of route happy-paths

---

## 🚀 SaaS Platform Plan — AI BDR for SMB B2B

> Comprehensive blueprint for turning the BMP Prospector into a sellable multi-tenant SaaS.
> Locked with Steve 2026-05-08:
>   1. **Shared Postgres DB** with `org_id` discriminator (Pipedrive/HubSpot pattern)
>   2. **Org-only tenancy** — no nested workspaces. Teams (later) are user labels.
>   3. **One codebase**, no fork. BMP becomes org #1.
>   4. **Platform-managed API keys** — AAMP holds the master keys for Anthropic, Netrows,
>      DataForSEO, Resend account, Twilio account, Blooio account. Customer orgs use them
>      under the hood; we meter consumption + bill back. Customers DO bring their own
>      verified email domain and their own phone numbers (provisioned through our master
>      Twilio/Blooio accounts), but they never see or manage API keys.
>   5. **Positioning**: AI BDR for small-to-mid B2B. Automate research + outreach +
>      appointment setting. "Tired of paying sales reps that can't set leads?"

---

### The North Star

A founder or sales manager signs up. In 10 minutes their CRM is populated with prospects,
the AI is generating personalized outreach across email/iMessage/LinkedIn, calls are being
scheduled into their iClosed/calendar, and they have a dashboard showing pipeline + revenue
forecast. They never set up Twilio, never configured Resend, never paid for an Anthropic
API key. We did all of that. They pay one monthly subscription that covers their seat +
usage allowances, with overages auto-billed.

That's the product. Everything below is in service of getting there without breaking,
without leaking customer data into the wrong org, and without losing money on cost
overruns.

---

### Core architectural decisions

#### 1. Multi-tenancy boundary
- **Org** = tenant. One company = one org. All CRM data scoped to org.
- **Users** belong to exactly one org. Cross-org access only for `super_admin` (AAMP staff).
- **Teams** (when added) are a `team` text label on User — used for routing & report filters.
  NOT a data partition. (Mirrors HubSpot Teams.)
- **`org_id`** is non-nullable on every tenant-scoped table. Indexed. Enforced by:
  - `scope_by_org()` helper required on every list query
  - JWT carries `org_id` alongside `user_id`
  - CI smoke test creates two orgs and asserts data is fully isolated across every list endpoint

#### 2. Platform-managed APIs (the key SaaS distinction)
The whole point: customer never sees a setup screen for Anthropic, Netrows, DataForSEO,
Resend, Twilio, or Blooio. They use AAMP's master keys; we meter and bill.

| Service | Master account holder | Per-org provisioned resource | Customer sets up | Metered as |
|---|---|---|---|---|
| **Anthropic** (Claude) | AAMP | nothing — shared key | nothing | tokens or generations |
| **Netrows** | AAMP | nothing — shared key | nothing | enrichments |
| **DataForSEO** | AAMP | nothing — shared key | nothing | audits |
| **Deepgram** (call transcripts) | AAMP | nothing — shared key | nothing | transcription minutes |
| **Resend** (email send) | AAMP | a verified Domain in AAMP's Resend account | DNS records (1-time, guided wizard) | emails sent |
| **Twilio** (calls) | AAMP | per-rep phone numbers under AAMP's Twilio sub-account-per-org | nothing — auto-purchase via wizard | call minutes + monthly per-number charge |
| **Blooio** (iMessage) | AAMP | dedicated number per org under AAMP's Blooio account | nothing — auto-provision | messages sent |
| **iClosed** | Customer's own | their existing iClosed account | one-time OAuth | nothing (their billing) |

**Why this works:**
- Onboarding goes from "set up 6 accounts and 8 API keys" to "fill in your company name and DNS-verify your email domain". Massive UX win.
- We get volume discounts on every API → margin opportunity
- We can swap providers without customer impact (e.g. Netrows → competitor)
- We control the abuse vector (one bad actor can't burn through our master Anthropic key
  because per-org rate limits + budget caps stop them well before that)

**Why this is risky and how we mitigate:**
- **Domain reputation cross-pollination on Resend** — one customer spamming hurts everyone's
  inbox placement. Mitigation: per-customer Resend Domain (each customer DKIM-signs from
  their OWN domain even though the API account is ours), automated bounce-rate monitoring
  with auto-suspend at >5% bounce rate.
- **TCPA exposure on Twilio** — one customer auto-dialing without consent → FCC complaint
  hits AAMP. Mitigation: A2P 10DLC registration owned by AAMP, customer onboarding includes
  TCPA agreement, send-window enforcement is mandatory (already built), do_not_call list
  imported per-org.
- **Blooio iMessage** — one customer abusing the channel could get ALL our orgs' iMessage
  capability throttled by Apple. Mitigation: separate Blooio dedicated number per org so
  abuse is contained to that number; rate limits per org per day.
- **Cost runaway** — bug somewhere causes 1M Anthropic calls. Mitigation: per-org daily
  spend cap on every metered service; alarm at 80%, hard-stop at 100%.

#### 3. Per-org domain provisioning (the only setup customer touches)
Customer signs up → we ask for their sending domain (e.g. `bymp.com`) → call Resend
Domains API to create the domain record under AAMP's Resend account → display the DNS
records they need to add (DKIM, SPF, return-path) → poll until verified → enable sending.

This is the ONE technical step the customer can't avoid (you can't impersonate someone's
domain in their absence — DKIM cryptographic proof is the whole point). We make it a 5-min
wizard with screenshots for the popular DNS providers (Cloudflare, GoDaddy, Namecheap, etc.).

For phone numbers: customer picks an area code → we hit Twilio Available Numbers API →
auto-purchase in our master account → assign to the rep. ~$1.15/mo per number, billed-back.

#### 4. Per-org `runtime_config` replaces the singleton
Today: `runtime_config(id=1)` holds API keys for the BMP install.
SaaS: `runtime_config(org_id, ...)` — one row per org. Holds:
- The org's Resend `from_domain` (the verified one)
- The org's Twilio sub-account credentials (for per-org phone-number scoping)
- The org's Blooio number ID
- Custom messaging direction (already shipped — moves to per-org)
- White-label branding (logo URL, primary color, signing-as name)
- Plan tier + monthly limits + usage counters

All API keys (Anthropic, Netrows, DataForSEO, Deepgram, base Resend account key, base
Twilio account key, base Blooio key) move to **platform-level `.env`** — never per-org.
Code that reads them goes through `app.platform_config.get_anthropic_key()` etc. instead
of `app.runtime_config.get_anthropic_key()`. Single source of truth.

---

### Data model — what changes

#### New tables

```python
class Organization(Base):
    """A customer account on the platform."""
    __tablename__ = "organizations"

    id              = Column(Integer, primary_key=True)
    slug            = Column(String(60), unique=True, index=True, nullable=False)  # 'bymp', 'acme-pool'
    name            = Column(String(255), nullable=False)                          # 'Backyard Marketing Pros'
    legal_name      = Column(String(255))                                          # for invoicing
    owner_user_id   = Column(Integer, ForeignKey("users.id"))                      # the founding user
    plan            = Column(String(40), default="trial")                          # trial / starter / growth / scale
    status          = Column(String(40), default="active")                         # active / suspended / cancelled
    trial_ends_at   = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=now_utc)

    # White-label
    logo_url        = Column(String(500))
    primary_color   = Column(String(20))                                           # '#1B5E20'
    signing_name    = Column(String(120))                                          # appears in audit reports / emails

    # Per-org sending identity
    send_domain     = Column(String(255))                                          # 'bymp.com' — verified in Resend
    send_domain_verified_at = Column(DateTime, nullable=True)

    # Twilio per-org sub-account (for phone-number scoping + cost attribution)
    twilio_subaccount_sid = Column(String(80), nullable=True)
    twilio_subaccount_token = Column(String(80), nullable=True)

    # Blooio per-org dedicated number
    blooio_number   = Column(String(40), nullable=True)                            # E.164
    blooio_number_id = Column(String(80), nullable=True)                           # Blooio's internal ID

    # Stripe billing
    stripe_customer_id     = Column(String(80), nullable=True, index=True)
    stripe_subscription_id = Column(String(80), nullable=True)

    # Spending caps (defense against runaway cost)
    monthly_spend_cap_cents = Column(Integer, nullable=True)                       # NULL = unlimited
    monthly_spend_alert_pct = Column(Integer, default=80)                          # email at this %
```

```python
class UsageEvent(Base):
    """One row per billable platform action. Aggregated nightly into UsageSummary."""
    __tablename__ = "usage_events"

    id          = Column(Integer, primary_key=True)
    org_id      = Column(Integer, ForeignKey("organizations.id"), index=True, nullable=False)
    kind        = Column(String(40), nullable=False, index=True)
    # kind values:
    #   'anthropic_generation'   — input + output tokens billed
    #   'netrows_enrichment'     — per call
    #   'dataforseo_audit'       — per audit
    #   'deepgram_minute'        — per minute of transcription
    #   'resend_email'           — per email sent (delivered)
    #   'twilio_call_minute'     — per minute of voice
    #   'twilio_number_monthly'  — flat $1.15/mo per assigned number
    #   'blooio_message'         — per outbound iMessage
    #   'blooio_number_monthly'  — flat per-month per dedicated number

    units       = Column(Float, default=1.0)         # tokens, minutes, count
    cost_cents  = Column(Integer, default=0)         # what AAMP paid (true cost)
    billed_cents= Column(Integer, default=0)         # what we charge the org
    metadata    = Column(Text)                       # JSON — message_id, call_sid, model name, etc.
    created_at  = Column(DateTime, default=now_utc, index=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=True)  # for per-rep attribution
```

```python
class UsageSummary(Base):
    """Monthly per-org rollup, keyed (org_id, year, month, kind). Cron job builds this
    nightly from UsageEvent so billing reads off a small table instead of millions of events."""
    __tablename__ = "usage_summary"

    id          = Column(Integer, primary_key=True)
    org_id      = Column(Integer, ForeignKey("organizations.id"), index=True, nullable=False)
    period_year = Column(Integer, nullable=False)
    period_month= Column(Integer, nullable=False)
    kind        = Column(String(40), nullable=False)
    units_total = Column(Float, default=0.0)
    cost_cents_total   = Column(Integer, default=0)
    billed_cents_total = Column(Integer, default=0)
    # (org_id, year, month, kind) is unique
```

```python
class Plan(Base):
    """Catalog of subscription plans with included allowances + overage pricing."""
    __tablename__ = "plans"

    id             = Column(Integer, primary_key=True)
    slug           = Column(String(40), unique=True)        # 'trial' / 'starter' / 'growth' / 'scale'
    name           = Column(String(80))
    monthly_price_cents     = Column(Integer)               # base subscription
    included_seats          = Column(Integer)               # users
    # Allowances per month (null = unlimited)
    allow_companies        = Column(Integer, nullable=True)
    allow_emails           = Column(Integer, nullable=True)
    allow_enrichments      = Column(Integer, nullable=True)
    allow_audits           = Column(Integer, nullable=True)
    allow_call_minutes     = Column(Integer, nullable=True)
    allow_imessages        = Column(Integer, nullable=True)
    # Overage pricing (cents per unit beyond allowance)
    overage_email_cents       = Column(Float, default=2.0)
    overage_enrichment_cents  = Column(Float, default=15.0)
    overage_audit_cents       = Column(Float, default=200.0)
    overage_call_minute_cents = Column(Float, default=4.0)
    overage_imessage_cents    = Column(Float, default=3.0)
```

```python
class DomainVerification(Base):
    """Resend domain setup status per org. Tracks DNS records the customer needs to add."""
    __tablename__ = "domain_verifications"

    id          = Column(Integer, primary_key=True)
    org_id      = Column(Integer, ForeignKey("organizations.id"), unique=True, index=True)
    domain      = Column(String(255), nullable=False)
    resend_domain_id = Column(String(80))
    records_json= Column(Text)                       # the DKIM/SPF records to display
    last_checked_at = Column(DateTime, nullable=True)
    verified_at = Column(DateTime, nullable=True)
    status      = Column(String(40), default="pending")  # pending / verified / failed
```

#### org_id added to every tenant-scoped existing table

`companies, contacts, deals, activities, tasks, generated_emails, page_views,
tracking_links, audit_reports, campaigns, searches, saved_views, runtime_config (becomes
per-org), call_ratings, sequence_steps, tags (probably per-org)` — non-null after backfill.

---

### Phased build-out

Each phase ends with a clear "Definition of Done" + a smoke test you can run.

#### Phase 0 — SQLite → Postgres (3-4 days, prerequisite)

Postgres unblocks everything. SQLite single-writer locking will collapse with multi-tenant load.

- [ ] Add `asyncpg` dependency
- [ ] `app/database.py` switch: `aiosqlite:///` → `postgresql+asyncpg://`
- [ ] Audit every migration script — they all use `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`
      (SQLite-specific). Rewrite as Alembic migrations OR Postgres-compatible plain SQL.
      Recommend: switch to **Alembic** during this phase. One-time cost; pays back immediately.
- [ ] Audit raw SQL in `merge` endpoints (companies + contacts) — `INSERT OR IGNORE` is SQLite
      syntax. Replace with `INSERT ... ON CONFLICT DO NOTHING`.
- [ ] One-shot data move script: `scripts/migrate_sqlite_to_postgres.py` reads existing
      `leads.db` and bulk-inserts into Postgres with order-preserving foreign keys.
- [ ] Staging deploy with copy of prod SQLite → verify all features work
- [ ] Production cutover: enable maintenance page, dump+import, swap DATABASE_URL, run app

**Definition of Done**: BMP runs identically on Postgres. All tests pass. Migration runs idempotently.

#### Phase 1 — Multi-org foundation (1 week)

- [ ] `Organization` model + `migrate_organizations.py` creates `id=1, slug='bmp', name='Backyard Marketing Pros'`
- [ ] `org_id` column on every tenant-scoped table (default = 1, then NOT NULL constraint added)
- [ ] **`scope_by_org(query, model, org)`** helper in `app/scoping.py` — reuses existing
      pattern. Every existing list query gets retrofitted.
- [ ] JWT payload extended: `{user_id, org_id}`. `get_current_user_in_org()` returns `(user, org)`.
      Old `get_current_user()` becomes a thin wrapper.
- [ ] Per-org `runtime_config` table (drop the `id=1` singleton constraint, add `org_id` PK
      with one row per org).
- [ ] `app/platform_config.py` — reads platform-level secrets from env (Anthropic, Netrows,
      DataForSEO, Deepgram, Resend, Twilio, Blooio master credentials). Replaces the
      `runtime_config.get_*_api_key()` calls for these services. Per-org config keeps only
      the tenant-specific values (send_domain, blooio_number, messaging_direction, branding).
- [ ] Super-admin "Switch Org" UI — top-bar dropdown showing all orgs, sets `current_org_id`
      in session for that browser tab.
- [ ] **CI smoke test** (`tests/test_org_isolation.py`): create org A and org B with users + data,
      log in as each, hit every list endpoint, assert response contains zero data from the
      other org. Run on every push.
- [ ] Audit subdomain (`audit.prospector.*`) routing: served by token (already works) but
      tokens are now scoped to org via the audit_reports row.

**Definition of Done**: BMP team uses the app exactly as before. Steve creates org #2 ("AAMP
Agency Internal") and adds a couple companies — they appear ONLY when he switches to that
org. Smoke test passes.

#### Phase 2 — Domain + phone provisioning wizard (3-4 days)

The customer-facing setup that turns a fresh org into a sending org.

- [ ] `POST /api/orgs/{slug}/domain/setup` — accepts `domain` field, calls Resend Domains API,
      creates `domain_verifications` row, returns DNS records.
- [ ] `GET /api/orgs/{slug}/domain/status` — polls Resend, marks verified when DKIM passes.
- [ ] Frontend wizard: 3 screens (enter domain → see DNS records with copy buttons + per-provider
      screenshots → "Verify" button polls).
- [ ] Send-domain selection per-org in `email_sender.send_email`: replace `settings.send_domain`
      with `current_org.send_domain` (with platform fallback during dev).
- [ ] `POST /api/orgs/{slug}/twilio/buy-number` — creates org's Twilio sub-account if missing,
      buys number, assigns to user. Stores monthly UsageEvent for the $1.15 charge.
- [ ] `POST /api/orgs/{slug}/blooio/provision-number` — provisions a dedicated Blooio number
      under AAMP's account (if API supports; otherwise document manual step).

**Definition of Done**: A new org slug created from scratch can verify a domain, buy a Twilio
number, and send an email + place a call within 10 minutes.

#### Phase 3 — Usage metering (3-4 days)

Wire UsageEvent rows into every billable code path. This is the data we need to bill.

- [ ] **`app/services/usage.py`** with `record(kind, org_id, units, cost_cents, billed_cents, metadata)`.
      Async, idempotent (dedup by external_id when possible).
- [ ] Hook into:
  - `email_generator.generate_*` — record `anthropic_generation` with input + output tokens
    (Anthropic API returns these in the response). cost = pricing × tokens.
  - `netrows_enrichment.*` calls → `netrows_enrichment` event
  - `audit_report.generate_report` → `dataforseo_audit` event (sum of inner DataForSEO calls)
  - `call_transcription` → `deepgram_minute` event
  - `email_sender.send_email` → `resend_email` event (only if Resend confirms 200)
  - Twilio voice status callback → `twilio_call_minute` event when call completes
  - Twilio number purchase → `twilio_number_monthly` event
  - Blooio send → `blooio_message` event
  - Blooio number provisioning → `blooio_number_monthly` event
- [ ] **Daily aggregator cron** (`scripts/aggregate_usage.py`): groups previous day's UsageEvent
      rows into per-(org, kind) UsageSummary rows. Idempotent.
- [ ] **Hard spend cap enforcement**: before any expensive operation, check
      `org.monthly_spend_cap_cents`; if exceeded → 402 Payment Required + email org admin.
- [ ] **Soft alerts** at 80% — daily check emails the org admin.

**Definition of Done**: Run sequences for a day, query `UsageSummary` and see exactly what
the org consumed broken down by kind, with cost + billable amount. Numbers reconcile
within 5% of the actual provider invoices.

#### Phase 4 — Stripe billing (4-5 days)

- [ ] `Plan` table + seed data (trial, starter, growth, scale)
- [ ] Stripe Customer + Subscription per org (created at signup)
- [ ] Stripe webhook receiver (`/api/billing/webhook`) — handles
      `customer.subscription.updated`, `invoice.payment_failed`, `invoice.paid`,
      `customer.subscription.deleted`
- [ ] **Monthly invoice** trigger: at month rollover, push UsageSummary overages to Stripe
      as `invoice_items` so the next subscription invoice includes overage charges
- [ ] Customer billing page: current plan, usage bars per metric, billing history,
      "Update payment method" button (Stripe Checkout)
- [ ] **Trial flow**: 14 days, then `subscription.deleted` triggers org status="suspended"
      (read-only — no sends, no enrichments, but data preserved)

**Definition of Done**: Sign up a test org with a real (Stripe-test-mode) card, run for a
"month" (cron-fast-forward), see the invoice land with base + overage. Cancel mid-cycle,
data freezes correctly.

#### Phase 5 — Self-serve signup + onboarding (3-4 days)

- [ ] Public landing page (separate static site or `/marketing` route on prospector domain)
- [ ] `POST /api/orgs/signup` — email + company name + password → creates Org + Owner User
- [ ] Email verification (one-time link) before sending becomes possible
- [ ] **Onboarding wizard** (replaces / extends the 10-step product tour for org owners):
      Step 1: Verify your domain (the wizard from Phase 2)
      Step 2: Pick a Twilio number for your first phone
      Step 3: Invite your team
      Step 4: Run your first prospect search
      Step 5: Generate your first audit report
- [ ] **Sample data option**: "Want some example prospects to play with?" — seeds 20 demo
      companies into the new org so they see the product working before they import their own.

**Definition of Done**: A stranger can hit the marketing site, sign up, verify domain, run
a search, generate an audit report, and send their first email — all without anyone from
AAMP touching their account.

#### Phase 6 — White-label (3-4 days)

- [ ] Org-level branding: `logo_url`, `primary_color`, `signing_name` apply across the app
- [ ] Audit reports use org's logo + colors (not BMP)
- [ ] Email signatures use the org's signing_name + Resend domain
- [ ] Custom subdomain: `customer.prospector.com` resolves to the customer's org (slug-based
      lookup at request time). Requires wildcard SSL cert (Let's Encrypt with DNS-01 challenge).
- [ ] Optional: customer-supplied custom domain (`crm.theiragency.com`) via CNAME +
      automated cert provisioning (Caddy or similar)

**Definition of Done**: A customer logs in to `customer.prospector.com`, sees their logo
in the top-left, generates an audit report → the report's banner shows their logo and
brand colors, not BMP's.

#### Phase 7 — Platform admin dashboard (2 days)

For AAMP staff (super_admin role) to monitor and support customers.

- [ ] List all orgs: name, plan, status, MRR, current month usage, last activity, owner email
- [ ] **"Login as"** button — issues a JWT with the org's user_id + a flag in the JWT payload
      that activities created during this session get tagged "via support". Audit Activity
      logged to the customer's org so they can see we logged in.
- [ ] Per-org usage drilldown: events table + summary with cost/billed split
- [ ] Revenue dashboard: total MRR, MRR by plan, churn rate, growth rate, trial-to-paid
      conversion
- [ ] **Health alerts**: orgs approaching limits, payment failed, inactive 7+ days,
      bounce rate >5%, abuse signals (high unsubscribe rate, complaints)

**Definition of Done**: AAMP can run support without ever asking a customer for their
password. Can spot a problem org before they churn.

#### Phase 8 — Compliance + hardening (ongoing, must-do before public launch)

- [ ] **Per-org rate limits** on every public endpoint (slowapi middleware keyed on org_id)
- [ ] **TCPA agreement at signup** — checkbox required, logged
- [ ] **DNC list import per org** — they upload, we check before dialing
- [ ] **GDPR data export per org** (`POST /api/orgs/{slug}/export-data`)
- [ ] **GDPR data deletion per org** (`POST /api/orgs/{slug}/delete-data`) — soft delete +
      30-day purge
- [ ] **Bounce-rate monitor**: org > 5% bounce rate → auto-suspend send → email admin
- [ ] **SOC 2 readiness**: encrypted DB at rest (Postgres native), TLS everywhere (already
      done), audit log of every admin action, password rotation policy, MFA for super_admin
- [ ] **Sentry** for error monitoring
- [ ] **Backup automation** per-org: daily pg_dump filtered by org_id → S3
- [ ] Terms of Service, Privacy Policy, AUP — drafted with a lawyer before public launch

**Definition of Done**: A customer's lawyer can review our docs and sign off. We can
demonstrate that org A has zero ability to access org B's data even at the DB level
(scope-by-org enforcement is auditable).

---

### Pricing — concrete cost math

**Cost per service (AAMP's wholesale):**
- Anthropic Sonnet 4: ~$3/M input tokens, $15/M output. Avg sequence email = ~1.5K input + 0.4K output = $0.011 per generation
- Netrows Starter: €49/mo for 1000 enrichments = $0.05/enrichment
- DataForSEO: ~$0.03 per audit (mix of endpoints)
- Deepgram Nova-2 telephony: $0.0043/min
- Resend: $20/mo for 50K emails = $0.0004/email
- Twilio voice: $0.013/min outbound, $0.0085/min inbound, $1.15/number/mo
- Blooio: $99/mo per dedicated number + per-message fee (TBD per Steve's contract)

**Plans:**

| Plan | Price | Seats | Companies | Emails | Enrichments | Audits | Call mins | iMessages |
|---|---|---|---|---|---|---|---|---|
| Trial (14 days) | $0 | 1 | 50 | 100 | 25 | 5 | 60 | 50 |
| Starter | $149/mo | 3 | 500 | 2K | 100 | 25 | 600 | 500 |
| Growth | $349/mo | 10 | 5K | 10K | 500 | 100 | 3K | 2K |
| Scale | $799/mo | 25 | 25K | 50K | 2K | 500 | 10K | 10K |

**Overage:**
- $0.02 per email (vs cost $0.0004 → 50× markup, normal for value-based pricing)
- $0.15 per enrichment (vs $0.05 → 3×)
- $2.00 per audit (vs $0.03 → 60× — the audit is THE differentiator, premium pricing)
- $0.04 per call minute (vs $0.013 → 3×)
- $0.03 per iMessage (vs ~$0.005 → 6×)

**Margin at Starter, mid-usage:**
Customer pays $149. Cost: ~50 generations × $0.011 + 75 enrichments × $0.05 + 15 audits ×
$0.03 + 1500 emails × $0.0004 + 400 call min × $0.013 + 5 numbers × $1.15 + Blooio fixed.
Total cost ≈ $25/mo. Gross margin ~83%.

**Break-even on infrastructure**: Postgres ($25) + Redis ($10) + hosting ($30) + S3 ($5) +
Sentry/monitoring ($20) = $90/mo fixed. Need 1 paying customer to break even on
infrastructure. Anthropic/Netrows/DataForSEO costs scale with revenue.

---

### Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| One customer's spam tanks Resend reputation for everyone | Medium | High | Per-customer Resend Domain (DKIM-signed from THEIR domain, not ours), bounce-rate auto-suspend |
| TCPA complaint hits AAMP master Twilio | Medium | High | A2P 10DLC owned by AAMP, mandatory TCPA agreement, send-window enforcement (built), per-org DNC import |
| Anthropic key leaks (e.g. via accidentally returning it in an API error) | Low | High | Never return key in any response; rotate quarterly; per-org spend cap stops runaway |
| Customer data leaked to wrong org via missed scope | Medium | Critical | scope_by_org() helper required + CI smoke test asserts isolation on every endpoint |
| Postgres connection exhaustion under load | Medium | High | PgBouncer in front, connection pool tuning, async everywhere already |
| Stripe webhook drops a `subscription.deleted` event → suspended customer keeps using product | Low | Medium | Idempotent webhook handler + nightly reconciliation cron pulling subscription state from Stripe |
| One customer abuses Blooio iMessage → Apple throttles all our orgs | Low | High | Per-org dedicated Blooio number, per-org daily message cap, abuse pattern monitoring |
| Cost blowup from buggy generation loop | Medium | High | Per-org daily spend cap, alarm at 80%, hard-stop at 100%, request rate limit per user |
| Customer churns mid-cycle but already burned through quota | Low | Medium | Stripe handles proration; we eat the difference (acceptable at our margins) |

---

### What's intentionally out of scope for v1

- Multi-region deployment (one US region is fine for years)
- SAML/SSO (paid add-on for Enterprise tier later)
- Custom workflows / Zapier-style automation builder (we're opinionated; the engine IS the workflow)
- Native mobile app (PWA covers it)
- Multi-currency billing (USD only)
- Self-serve SSO/SCIM (Enterprise tier hand-holds)
- Multi-org users (one user = one org for v1; "switching orgs" needs a separate user record per org)

---

### Convention: how new BMP features get into the SaaS automatically

Because the codebase is shared (no fork), every new BMP feature works for SaaS customers as soon
as it's built — provided the developer follows the rules:

1. **Never read API keys from env directly outside `app/platform_config.py`.** That's the
   single dependency point.
2. **Always scope list queries with `scope_by_org()`.** Never write `select(Company).all()`.
3. **Never reference `org_id=1` as a hardcoded value.** Get it from the current user's JWT.
4. **Never write to the singleton `runtime_config` row.** Use the per-org config helpers.
5. **Public endpoints (no auth)** — webhook receivers, click trackers, the page-view beacon —
   must derive `org_id` from the URL path (`/t/{token}` → look up token → get its org_id) or
   from the inbound metadata (Twilio webhook → look up phone → which org owns this number).
   No assumptions about a default org.
6. **Every new migration** that touches an existing table must add `org_id` if the table is
   tenant-scoped. Backfill = current org of any existing rows.

Wire these into a pre-commit hook OR a documented PR checklist. Skipping them is what kills SaaS
products — silent cross-org leaks that no one notices for months.

---

### Migration path — concrete week-by-week

**Week 1**: Phase 0 (Postgres). BMP runs on Postgres. No multi-tenancy yet. Everything works.

**Week 2**: Phase 1 (multi-org foundation). BMP becomes org #1. Steve creates his other
companies as orgs #2, #3, #4. He uses them in real life for a week — this is the dogfood test.

**Week 3**: Phase 2 + 3 (provisioning + metering). Steve's other orgs verify their own domains
and buy phone numbers through the wizard. Usage events flowing into the table; we can SEE per-org
costs.

**Week 4**: Phase 4 (billing). Steve sets up a paid plan for his other companies (test mode).
Stripe pulls overages correctly. Trial flow works.

**Week 5**: Phase 5 (signup + onboarding). Open invitation-only beta — 5-10 friendly customers
who agree to ride the early bumps. They come through the public signup flow.

**Week 6**: Phase 6 (white-label). The friendly customers get their logo + colors + custom
subdomain.

**Week 7**: Phase 7 (admin dashboard). AAMP team can support customers without DB access.

**Week 8 onwards**: Phase 8 (compliance + hardening). Iterate on what beta customers hit.
Public launch when confident — likely 10-12 weeks total from start.

---

### First steps when we kick off

1. **Provision a Postgres database** (Supabase or Railway free tier is fine for dev)
2. **Create `tests/` directory + first smoke test** — even before we change anything. This
   catches regressions during the migration.
3. **Phase 0 starts with `requirements.txt` + `app/database.py`.** Everything else is mechanical
   from there.

No fork. No new repo. Just keep building on `main` and the SaaS comes for free at the right
seam in the code.

---

## 🧭 Guided Onboarding Walkthrough (10-Step Product Tour)

> Like HubSpot, Monday.com, Canva — a step-by-step guided tour that walks new users through
> the entire platform the first time they log in. They just keep hitting "Next" and it highlights
> each feature in context.

### How It Works

When a new user logs in for the first time (or an admin resets their tour), a modal overlay
walks them through the platform one step at a time. Each step:
- Highlights a specific UI element (spotlight/tooltip style)
- Explains what it does and why they'd use it
- Has a "Next" button to advance and a "Skip Tour" to bail out
- Progress bar shows "Step 3 of 10"

### The 10 Steps

| Step | Screen | Highlight | What They Learn |
|------|--------|-----------|-----------------|
| 1 | Dashboard | KPI strip | "This is your command center — MRR, active deals, emails sent, response rate at a glance" |
| 2 | Companies | Search bar + filters | "Search Google Maps for prospects by industry and location. Filter by review count to find established businesses" |
| 3 | Companies | "Add to Pipeline" button | "Found a prospect? Hit this button to enrich their data, find contacts, generate an audit report, and create a deal — all in one click" |
| 4 | Company Detail | Three-column layout | "Left panel: company info and enrichment data. Center: contacts and email sequences. Right: timeline of all activity" |
| 5 | Company Detail | Sequence panel | "Each contact gets a multi-channel sequence — emails, LinkedIn, iMessage, calls. Steps auto-send on schedule or you can send manually" |
| 6 | Pipeline | Kanban board | "Drag deals between stages. Cards show value, company, and days in stage. Click any card to jump to the company" |
| 7 | Pipeline | Deal card actions | "Snooze deals that aren't ready yet — they'll wake up automatically and create a follow-up task" |
| 8 | Contacts | Contact list + filters | "All your contacts across companies. Filter by email status, phone type, or sequence state. Click to see their full profile" |
| 9 | Audit Reports | Sample report | "AI Findability Audits are your lead magnet. Every prospect gets one — share the link in emails and messages. The competitor comparison is gated behind a discovery call booking" |
| 10 | Dashboard | Activity feed + calls | "Track everything your team does. Call recordings get AI transcription and coaching summaries. Your manager can review and rate calls" |

### Technical Implementation

**Option A — Lightweight (build it ourselves):**
- Store `onboarding_step` (int, 0-10) on the User model. 0 = not started, 10 = complete.
- Pure JS overlay system in index.html — no library needed
- Each step is a positioned tooltip with a spotlight mask (CSS `box-shadow` trick)
- "Next" button increments the step, saves to API, shows the next tooltip
- "Skip Tour" sets step to 10
- Admin can reset a user's tour via user management

```javascript
// Core concept
const TOUR_STEPS = [
    { target: '#kpi-strip', title: 'Your Dashboard', text: '...', position: 'bottom' },
    { target: '#company-search', title: 'Find Prospects', text: '...', position: 'bottom' },
    // ... etc
];

function showTourStep(stepIndex) {
    const step = TOUR_STEPS[stepIndex];
    const el = document.querySelector(step.target);
    // Position tooltip near element, add spotlight overlay
    // "Next" calls showTourStep(stepIndex + 1)
    // Save progress: fetch('/api/users/me/onboarding', { method: 'PATCH', body: { step: stepIndex } })
}
```

**Option B — Use a library:**
- [Shepherd.js](https://github.com/shepherd-pro/shepherd) — MIT, 12KB, exactly this use case
- [Intro.js](https://introjs.com/) — popular but commercial license for SaaS
- [Driver.js](https://driverjs.com/) — MIT, lightweight, good spotlight effect

**Recommendation:** Start with Option A (pure JS) since we're already vanilla JS. It's maybe
100 lines of code and zero dependencies. If it gets complex, swap in Shepherd.js later.

### What Needs to Happen

1. Add `onboarding_step` column to User model (default 0)
2. Add `PATCH /api/users/me/onboarding` endpoint to save progress
3. Build the overlay/tooltip system in index.html
4. Write copy for each of the 10 steps
5. On login, if `onboarding_step < 10`, auto-start the tour
6. Add "Restart Tour" button in user settings/profile

### For SaaS Version

The same tour system works for SaaS customers, but the steps would be slightly different:
- Step 1 becomes "Welcome to [OrgName]" with their branding
- Add a step for "Invite your team" (not needed for BMP since admin adds users)
- Add a step for "Connect your email domain" (Resend setup)
- The 10 steps become configurable per-org if they want to customize for their team

### Why This Matters

- BDRs going live tomorrow won't need hand-holding — the platform teaches itself
- Reduces support burden as you scale the team
- Critical for SaaS — you can't personally onboard every customer
- Increases activation rate (users who complete onboarding stick around)

---

## ✅ Shipped 2026-05-08 (continuing session — Steve stepped away with approval)

Seven commits landed end-to-end. Order shipped:
1. **Credit meter shim** (ca8fc50) — `credit_ledger` table, `credit_meter.meter()`,
   `meter_standalone()`, idempotency-keyed dedupe, two-layer schema
   (customer credits + raw vendor cost). Wired into Resend sends + Netrows + Hunter.
2. **iClosed gate widget fix** (880fa4c) — replaced the phantom `book_call(slot_time="")`
   integration with a real embedded iClosed iframe. Self-confirm "I've Scheduled"
   button + email capture. `audit_reports.booked_at` + `booked_email` columns +
   webhook stub at `/api/iclosed/webhook`.
3. **iClosed everywhere** (4e07805) — signature falls back to org iClosed URL,
   prominent CTA in audit report header + bottom (replaced "Let's Talk → /contact"),
   Settings UI gets a "Use team iClosed link" quick button, webhook secured with
   `?t=<secret>` shared-secret guard, idempotent migrations auto-run on startup.
4. **AI gen + SMS metering** (9b0ca4e) — wired `meter_standalone` into all 5
   `email_generator.py` AI calls + `call_transcription.py` summary call +
   `twilio_sms.py` send + `blooio_messaging.py` iMessage send.
5. **Twilio Lookup metering + eager populate** (cc4e61d) — `lookup_phone_type`
   meters every call as `phone_lookup`. Manual contact create now eagerly fetches
   `phone_type` so badge appears on next page load.
6. **Reply sentiment classification (D1)** (61dc7fa) — every inbound reply gets
   AI-classified into 6 sentiment buckets + a one-line gist. Background async
   so webhook stays fast. Colored timeline badges + dashboard activity feed
   integration.
7. **Email verification hard gate (D4)** (5f4babd) — `email_validation.ensure_email_validated()`
   gates every send (sequence engine + manual). Caches Hunter result on
   `contact.email_status`. Fail-open policy when Hunter is down.

### Operator follow-ups Steve should handle on next deploy
1. `./scripts/deploy.sh` — picks up all seven commits.
2. (Optional) Add to systemd `ExecStartPre` chain on VPS:
   ```
   ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_credit_ledger
   ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_audit_booked
   ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_reply_sentiment
   ```
   `init_db()` already auto-runs the audit_booked + reply_sentiment migrations
   on lifespan startup, so this is belt-and-suspenders. credit_ledger is a
   brand-new table created by `Base.metadata.create_all`, no migration needed.
3. Set `ICLOSED_WEBHOOK_SECRET=<long-random>` in `/opt/backyard-leads/.env`.
4. In iClosed: change webhook URL from `/api/iclosed/webhook` to
   `/api/iclosed/webhook?t=<that-same-secret>`.
5. Smoke-test: send any email → check Settings → Credits & Usage panel
   shows the row. Run an enrichment → confirm Netrows/Hunter rows appear.
   Visit any audit report → confirm new "Schedule a Discovery Call" button.
   Reply to an outreach email → confirm the timeline shows a colored
   sentiment badge ~10 seconds after the reply lands.

### Next session — locked priority list
- **Lead scoring v2** — fit (firmographics) × intent (engagement + sentiment + line_type).
  Replaces the "3+ opens" Hot Leads heuristic with a real model. Phone-type and
  reply sentiment data are now flowing, so this is the right time. Est. 1-2 hours.
- **God Mode** — campaigns model already supports `business_types` + `locations` as
  JSON lists; runner currently caps at one location per pair. Lift the cap +
  add per-target weights + add the morning-brief generator for overnight runs.
- **Apollo BYO-key adapter** — first SaaS-only provider. Refactor existing
  Netrows/Hunter into adapter pattern, add Apollo class. Multi-day lift.
- **Morning brief** — TZ-aware 7am cron, per-user digest. Needs God Mode in place
  so the "while you slept" section has data to summarize.
- **AI chatbot** — Sonnet tool-use widget. Multi-day lift.

---

## 🎯 SaaS-readiness initiatives (locked 2026-05-08 with Steve)

After the strategic review, Steve picked these for the build queue. Order is
the recommended sequence; each item links to its design notes below.

### Picked — Foundation (A/B/C — must come first)
- **A. Enrichment waterfall + provider-adapter architecture** — `EnrichmentProvider` interface, configurable cascade per tenant. Unblocks Apollo BYO-key.
- **A2. Twilio Lookup integration** — landline/mobile/voip detection on every phone we collect. New `phone_line_type` column on Contact. Cost ~$0.005/lookup. Required for safe SMS + safe voice dial + lead scoring.
- **B. Apollo BYO-key adapter** — first SaaS-only provider, tenant supplies key, we eat zero per-record cost. Proves the adapter pattern.
- **C. Credit metering + cost-per-action ledger** — required before any of the above can be billed. New `credit_ledger` table; every billable action emits a row.
- **C2. Admin cost-of-goods dashboard** — platform-side view: what does each tenant cost us in raw spend (Resend + Twilio + Anthropic + provider APIs)? Shows margin per tenant. Critical for SaaS unit-economics visibility.

### Picked — D-list (CRM features)
- **D1. Reply sentiment classification** — auto-tag every inbound reply
- **D3. Send-time optimization** — per-contact best send hour, learned
- **D4. Email verification before send (hard gate)** — Hunter verify required, not optional
- **D6. Deliverability dashboard** — bounce rate, complaints, blacklist status per domain
- **D7. Lead scoring (fit × intent)** — replaces "3+ opens" heuristic with real model. Now also incorporates `phone_line_type` (mobile = higher contactability score).
- **D8. Bulk import wizard** — column mapping UI, replaces rigid CSV
- **D9. Audit log** — required for SOC2 + enterprise sales
- **D10. Public API + Zapier** — table stakes for SaaS

### Picked — God Mode (E)
Refined per Steve: **multi-vertical + multi-geo portfolio**, runs forever
until paused. Existing Autopilot caps to one (vertical, geo); God Mode
makes it a list. Design notes in section below.

### Picked — Morning brief email
Per-user daily digest: overnight summary, today's priorities, hot replies,
weekly stats, AI-flagged insight. Sent via Resend at user's TZ-aware 7am.

### Picked — AI chatbot (corner widget)
Conversational query interface. Sales rep chats with the DB. "Find me hot
leads in Phoenix I haven't contacted in 7 days." Anthropic tool-use under
the hood. Yes, this is very buildable.

---

### 💰 Credit metering + cost-of-goods — design

**The two layers** (don't conflate them):

1. **Customer-facing credits.** Tenants buy credit packs. Every billable
   action burns N credits. They see balance, burn rate, projected runway.
2. **Platform-internal cost-of-goods.** What we actually pay vendors per
   action. Admins (Steve / staff) see margin per tenant, total platform
   COGS, alerts on outliers. Customers never see this.

**Schema:**
```
credit_ledger
  id, company_id, user_id, action_type, action_ref,
  credits_debited, raw_cost_usd, vendor (resend/twilio/anthropic/apollo/...),
  created_at, idempotency_key

credit_balance  (one per company)
  company_id, balance_credits, monthly_included, last_topup_at, next_reset_at

action_pricing  (rate card, editable by admin)
  action_type, credits_per_unit, raw_cost_estimate_usd, last_updated
```

**Action types to meter (initial set):**
| action_type | Vendor | Raw cost | Credits |
|---|---|---|---|
| email_send | Resend | $0.0004 | 1 |
| email_verify | Hunter | $0.04 | 8 |
| ai_email_gen | Anthropic | $0.005 | 2 |
| ai_chat_turn | Anthropic | $0.015 | 5 |
| ai_reply_classify | Anthropic | $0.001 | 1 |
| enrich_apollo | Apollo (BYO) | $0 to us | 0 (tenant key) |
| enrich_netrows | Netrows | €0.05 | 10 |
| enrich_hunter | Hunter | $0.04 | 8 |
| phone_lookup | Twilio Lookup | $0.005 | 1 |
| sms_send | Twilio | $0.008 | 2 |
| voice_minute | Vapi/Retell | $0.10 | 20 |
| scrape_yelp | (compute) | $0.001 | 1 |
| scrape_maps | (compute) | $0.001 | 1 |

**Customer dashboard widgets:**
- Balance + projected runway ("at current burn, 14 days left")
- Burn-by-action pie ("60% enrichment, 25% sends, 10% AI gen, 5% other")
- Top-spending campaigns
- Top-spending users (admin only)

**Admin (platform staff) cost-of-goods dashboard:**
- COGS per tenant (last 30d, MoM trend)
- Margin per tenant (revenue from credit packs - raw COGS)
- Top vendors by spend (where's our money going)
- Outlier alerts ("Tenant X spent 3× their plan this week")
- Per-action unit-cost trend (catch vendor price changes)

**Idempotency.** Every meter call must take an `idempotency_key` so retries
(e.g., a re-fired sequence step) don't double-charge. Use the action's
natural ID (e.g., `email_send:{generated_email_id}`).

**Implementation footprint:**
- `app/services/credit_meter.py` — `meter(company, user, action_type, ref, idempotency_key)`
- Wrap every existing billable call site (Resend send, Anthropic call, Netrows lookup, etc.) with `meter()`
- New routes `/api/me/credits/*` and `/api/admin/cogs/*`
- New dashboard panel for tenants + new admin-only view

**Migration order:**
1. Build the meter as a no-op shim first (just logs to ledger, doesn't enforce)
2. Run for 1-2 weeks to observe real costs vs. estimates → tune the rate card
3. Flip enforcement on (out-of-credits → block action)

This gives us live cost data BEFORE we have to set retail prices, so the
SaaS launch pricing is grounded in reality.

---

### 📞 Twilio Lookup — design

**What.** Twilio's Lookup API tells you, for any phone number:
- `line_type` — mobile / landline / voip / fixed_voip / unknown
- carrier name
- caller name (US only, ~$0.01 surcharge)
- whether the number is even valid

**Pricing.** Basic validation = $0.005/lookup. Line type = $0.008. Caller
name = $0.01. We'd run validation + line_type as the default; caller name
on demand only.

**Where it plugs in:**
1. **On every new Contact phone field write** — async background job, sets
   `contact.phone_line_type` + `contact.phone_carrier`
2. **On bulk import** — run lookup pass after CSV import, before any send
3. **On any sequence step that uses phone** — gate SMS/voice channels
   based on line_type
4. **On scrape/enrichment** — confirm phones we mined from web/Yelp are real
5. **Pre-Voice-AI dial** — hard requirement, won't dial mobiles for cold

**Schema additions:**
```
contacts
  + phone_line_type (varchar 20, nullable)
  + phone_carrier (varchar 100, nullable)
  + phone_validated_at (timestamp, nullable)
  + phone_valid (boolean, nullable)  -- false → suppress
```

**Cost in practice.** At $0.005/lookup × 30 contacts/day under God Mode =
$0.15/day per active campaign. Negligible. Charge tenants 1 credit per
lookup (50% margin).

**Failure modes.**
- Twilio Lookup occasionally returns `unknown` — treat as "do not assume
  mobile, do not assume landline". UI surfaces as a warning.
- VoIP numbers — TCPA treats them like mobile for SMS, but voice rules
  vary. Default to "treat as mobile" for compliance.

---

### 🛰️ God Mode — multi-vertical / multi-geo design

**The shape.** A God Mode campaign is a *portfolio* of targets, not a single
target. Each portfolio has many `(vertical, geo)` pairs that all run
concurrently under one daily budget cap.

**Schema (proposed):**
```
god_mode_campaigns
  id, company_id, owner_user_id, name, status (running/paused),
  daily_budget_credits, daily_max_new_contacts, daily_max_sends_per_sender,
  channel_mix (email/sms/voice json), sequence_id (which sequence to enroll into),
  created_at, paused_at, paused_reason

god_mode_targets  (one per vertical+geo pair, many per campaign)
  id, campaign_id, vertical, geo_city, geo_state, geo_radius_miles,
  weight (1-10, for budget allocation), scrape_cursor (offset/page state),
  status (active/exhausted/paused), last_run_at,
  total_contacts_enrolled, total_credits_spent

god_mode_runs  (one per nightly tick, drives morning brief)
  id, campaign_id, started_at, finished_at,
  contacts_enrolled, sends_made, replies_received,
  credits_spent, error, summary_json
```

**Daily budget allocation across targets.**
- Round-robin by default (each target gets equal share)
- OR weighted: `target.weight` determines share of `daily_budget_credits`
- Per-target spend cap = `(weight / sum_weights) * campaign.daily_budget_credits`
- When a target hits its cap, it sleeps until tomorrow but others keep running

**Per-target guardrails.** A spam-complaint spike in one geo pauses *that
target*, not the whole campaign. Bounce rate >3% on a target = auto-pause +
alert. This is the key win over the current Autopilot.

**Cursor management.** Each target tracks where it left off in the scrape so
we never re-process the same Yelp/Maps results. When a target exhausts
(no new results for N runs), mark `status=exhausted` and surface in the brief
("Phoenix pool builders is tapped out — add new geo or expand radius").

**UI sketch.** New page `God Mode → Campaigns`. Each campaign shows a table
of targets with live counters (enrolled today, replies today, credits spent
today, status). Add Target button → vertical dropdown + geo picker (with
multi-select map). Master Pause/Resume button.

**Channel-mix-over-time.** Email is primary. SMS day 5 if no reply. Voicemail
day 9 if no reply. Configurable per campaign. This is just sequence template
selection at enroll time — most logic already exists.

---

### ☀️ Morning Brief — daily digest email

**Trigger.** Cron tick every 15 min checks for users whose local time is
their configured brief hour (default 7am). One email per user per day.

**Sections (in order):**
1. **Overnight** — what God Mode + sequences did while you slept
   ("28 contacts enrolled, 47 emails sent, 4 replies, 1 booked, $12 spent")
2. **Today's priorities** — top 5 tasks due today, hot leads needing call,
   stuck deals you own
3. **Inbox replies** — quick list of replies awaiting human action with
   sentiment tag (interested / objection / OOO) and one-click links to view
4. **AI insight (one item)** — single highest-value observation from your data
   ("Smith Pools opened your email 4× yesterday — schedule a call")
5. **Weekly stats** — sends, opens, replies, books vs last week (mini-trend)
6. **Footer** — manage brief settings · snooze 7 days · unsubscribe

**Personalization.**
- Scoped to user (sees only what their role permissions allow)
- TZ-aware delivery — store user's timezone in `users.timezone`
- Skip on weekends if `users.brief_weekends = false`

**Implementation footprint.**
- New route `/api/me/brief/preview` — render the brief for current user
- Cron job in existing scheduler (every 15 min check user.brief_send_at)
- New table `brief_sends` (idempotency: one per user per day)
- AI insight is one Anthropic call per user per day, ~$0.01 each
- HTML template at `app/templates/morning_brief.html`

**Why this matters strategically.** This is the SaaS retention play.
Customers who open daily emails *don't churn*. It also creates a "magic
moment" — they wake up to a smart digest and feel the AI working for them.

---

### 🤖 AI chatbot ("Ask BMP") — conversational query widget

**The shape.** Floating bottom-right widget, expands to a chat panel.
Sales rep types in plain English; the bot queries the database via
Anthropic tool-use and returns scoped, role-aware answers.

**Sample queries the v1 should handle:**
- "Find hot leads in Phoenix I haven't contacted in 7 days"
- "What's my pipeline value for closed-won this quarter?"
- "Who replied this week and what did they say?"
- "Show me pool builders rated 4.5+ we haven't enrolled yet"
- "Summarize Smith Pools — full thread plus deal status"
- "Which sequence has the best reply rate this month?"
- "Draft a casual follow-up to John at Acme using their last reply"

**Architecture.**
```
[Widget UI]  ←streaming SSE→  [POST /api/ai/chat]
                                    │
                                    ▼
                       Claude Sonnet 4.6 + tool-use
                                    │
                ┌───────────────────┼─────────────────────┐
                ▼                   ▼                     ▼
       search_contacts()    search_companies()    search_deals()
       get_activity()       get_pipeline_stats()  draft_email()
       (all tools wrap Scope(user) — tenant + role enforcement)
```

**Tool design (the safety-critical part):**
- NO raw `run_sql` tool. Each tool is a typed, scoped query helper.
- Every tool's first line: `q = Scope(user).filter(BaseQuery)` — multi-tenant + role isolation enforced at the tool level, not relied on at the model level
- Return structured JSON, never raw rows; redact fields the user can't see
- Hard cap on result size (50 rows max per tool call)
- Conversation logs stored for audit + future fine-tuning

**Cost model.**
- ~3-5k input tokens per turn (system prompt + tools + history)
- ~500-1k output tokens per turn
- Sonnet pricing: ~$0.005-0.02 per turn
- Suggest charging 5 credits per turn (50% margin) — "AI Assistant credits"

**v1 scope (3-4 days):**
- Widget UI with streaming
- 6 read-only tools (search_contacts, search_companies, search_deals,
  get_activity, get_pipeline_stats, summarize_entity)
- Conversation memory client-side, stateless server
- Basic guardrails (rate limit, role scoping, redaction)

**v2 (later):**
- Write actions (create_task, send_message, enroll_in_sequence) with
  confirmation step ("Enroll these 8 contacts in Pool Builder Sequence A?")
- Voice input (whisper) for dictation
- Cross-conversation memory ("remember I prefer terse responses")

**Why this is buildable.** Anthropic tool-use is mature; we already use
Claude for email gen. The constrained tool surface (no raw SQL) makes the
multi-tenant scope problem tractable. Biggest risk is prompt injection
through user-supplied data (contact names with `</tool_use>` etc.) — mitigate
with strict tool-result sanitization.

---

## 🎙️ AI Voice — agentic BDR exploration (added 2026-05-08)

> Added by Steve: voice models have gotten good enough that a fully AI-driven
> outbound agent is plausibly within reach. This is exploration, not a commit.

### The thesis
A truly agentic BDR system runs three lanes — email, SMS, voice — with the same
sequence engine driving all three. Voice is the lane we have not built. If the
voice agent can: (a) place an outbound call, (b) handle the gatekeeper, (c) ask
3-5 qualifying questions, and (d) book a meeting OR transfer to a human — we
have a closed-loop AI BDR that can run on God Mode (see SaaS plan section).

### Vendor landscape (research-only, no decision yet)
| Vendor | Model | Pricing rough | Strengths | Weak spots |
|---|---|---|---|---|
| **Vapi** | BYO-LLM, ElevenLabs/Deepgram TTS | ~$0.05–0.12/min all-in | Best dev UX, function calling, low latency | You assemble the stack |
| **Retell** | Hosted end-to-end | ~$0.07–0.15/min | Plug-and-play, good defaults | Less flexible |
| **Bland.ai** | Hosted | ~$0.09/min, enterprise tiers | Phone numbers + dialer included, scales hard | Pricier, opinionated |
| **ElevenLabs Conversational** | EL voice + LLM | ~$0.08/min | Best-in-class voice quality | Newer, less feature-rich |
| **Deepgram Voice Agent** | Deepgram STT/TTS + BYO-LLM | ~$0.06/min | Lowest latency, good for live transfer | DIY orchestration |

### Use cases (in order of risk/value)
1. **Voicemail drop** — lowest risk, no live convo. Detect VM via Twilio AMD,
   play a personalized 20-sec message generated from the prospect's company
   data. Cheap, compliant, scalable. **Start here.**
2. **Warm dialer / transfer** — agent makes the call, qualifies for 60 sec,
   then transfers to a human BDR. Human handles close. Reduces BDR time-on-dial.
3. **Full agent** — agent handles entire convo end-to-end, books via iClosed
   API. Highest reward, highest compliance/quality risk.

### Compliance & deliverability landmines
- **TCPA** — no autodialed calls to mobile numbers without prior express written
  consent. Cold calling cell phones with an AI dialer is a $500–$1,500 per-call
  liability. We need a clean landline filter (Twilio Lookup line_type) before
  any AI dial. **This is the single biggest blocker.**
- **State-specific**: FL, OK, MD have stricter "mini-TCPA" laws. WA requires
  AI disclosure on the call. CA CCPA-adjacent rules around recordings.
- **AI disclosure** — federal FCC ruling (Feb 2024) treats AI-generated voice
  in robocalls as illegal under the TCPA absent consent. Disclosure ("This is
  an AI assistant calling on behalf of...") is now table stakes; we should bake
  it into the system prompt.
- **Recording consent** — two-party-consent states (CA, FL, IL, MD, MA, MT,
  NH, PA, WA) require both sides to consent before recording. Either get
  consent at the top of the call or don't record those states.
- **Suppression** — DNC list scrub before any dial. Federal + state lists.

### Architecture sketch (when we build it)
```
sequence_engine.py
  └── _handle_voice(step)
        ├── compliance_check(contact)        # mobile? DNC? state rules?
        ├── twilio_lookup(phone)              # landline confirmation
        ├── voice_provider.start_call(...)   # Vapi/Retell/Bland
        │     └── webhook on call complete
        │           ├── log Activity (transcript, duration, outcome)
        │           ├── if booked → create Deal/Task
        │           ├── if not_interested → close lost + suppress
        │           └── if voicemail → log + advance sequence
        └── credit_meter.charge(minutes * cost_per_min)
```

### Cost modeling for SaaS pricing
- 60 sec call ≈ $0.06–0.15 voice + $0.01 Twilio = ~$0.10/call
- A 1,000-dial daily campaign = ~$100/day per tenant in raw cost
- Need a "voice credits" SKU separate from email/AI credits — voice burns 10-30x faster
- Suggested package: 500 voice minutes/seat/mo standard, overage $0.20/min retail (50-100% margin)

### What we'd need before building
1. Twilio Lookup integration for landline filtering (cheap, ~$0.005/lookup)
2. DNC scrub vendor (RealPhoneValidation, NumVerify, or DNC.com API)
3. State-by-state compliance config table (per-state opt-in rules)
4. Recording consent flow with state-aware system prompt injection
5. Voice-credit ledger added to the credit metering system (which itself doesn't exist yet)

### Recommended path
- **Phase 1** (1-2 days): Voicemail drop only. Twilio AMD + ElevenLabs
  pre-recorded personalized VMs. No live convo, minimal compliance surface.
- **Phase 2** (3-5 days): Vapi-driven warm dialer with human transfer to
  iClosed-booked BDR. Pilot internally on warm leads only (replied → no-show).
- **Phase 3** (2+ weeks): Full agent with booking authority. Only after
  Phases 1-2 prove out and compliance scaffolding is solid.

### Open questions for Steve
- Are we OK with starting voicemail-only as a wedge, or do we want to skip to live-agent?
- What's the appetite for restricting voice dial to landlines only at launch (huge addressable market shrink, but kills the TCPA risk)?
- Do we want voice as a BMP-only feature first, or wait to ship it inside SaaS?

---

## 🔴 Closed / decided
- Apollo evaluated → keeping integration code but it's effectively dead for SMB; Netrows replaces it
- Coresignal evaluated → rejected (LinkedIn-derived, same blind spot as Apollo)
- OpenCorporates evaluated → rejected (real commercial pricing $400+/mo, free tier is non-commercial only)
- Bizapedia / state SoS scraping → skipped (Netrows decision-maker covers same ground better)

---

## Conventions you and I have agreed on (don't lose)
- **GitHub `main` is the source of truth.** Always `git pull --ff-only origin main` at session start.
- **Deploy with `./scripts/deploy.sh`** from any machine with SSH access to the `vps` host alias. Pre-flight JS parse-check is built in.
- **Migrations** live in `scripts/migrate_*.py`, are idempotent, and chain via systemd `ExecStartPre`.
- **Cache-Control: no-store** on `/` so browsers don't serve stale HTML after a deploy.
- **`docs/netrows-openapi.json`** — full Netrows OpenAPI spec saved locally (273 endpoints) for offline reference.
