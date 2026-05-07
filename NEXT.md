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

### 🔥 Next priority: Missive Integration

**Phase 1 — Missive webhook (1-2 days):**
- Missive webhook receiver at `/api/missive/webhook`
- When email arrives from a known contact → auto-log to CRM timeline
- Auto-pause the active sequence for that contact
- Auto-set company status to "replied"
- Zero BDR behavior change — just works in background

**Phase 2 — Missive sidebar app (1 week):**
- Sidebar app hosted at `/missive-sidebar`
- Shows company/contact card when BDR opens an email
- Sequence status, deal info, problems found
- "Mark Replied" / "Add Note" / "Open in CRM" buttons
- BDR sees CRM context without leaving Missive

**Phase 3 — Full Missive send integration (future):**
- Send FROM Missive instead of Resend
- Sequence creates draft in Missive, BDR reviews and sends
- Full two-way sync — every email in/out logged
- Eliminates Resend dependency for sending

**Architecture:**
```
Outbound: Prospector → Resend → steve@go.backyardmarketingpros.com → Prospect
Reply:    Prospect → steve@backyardmarketingpros.com → Missive → Webhook → Prospector
Sidebar:  Missive iframe → prospector.backyardmarketingpros.com/missive-sidebar
```
Missive API docs: missiveapp.com/help/api

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

### 🔥 NEXT SESSION (2026-05-07 plan, locked with Steve)
- [ ] **Missive Phase 1** — webhook receiver at `/api/missive/webhook`. Inbound email from Missive → match to a contact → log to timeline + auto-pause sequence. Highest ROI of the new builds because Missive replies are currently invisible to the engine.
- [ ] **Google OAuth** — for two reasons:
  - Send email from the user's Gmail account (replaces Resend for those who want native Gmail send)
  - Read calendar availability (precondition for the scheduling tool below)
  - Probably wire `Sign in with Google` for user auth too while we're in there
- [ ] **Manual "Send this step now" button** in the sequence panel — currently the engine fires steps when their `scheduled_send_at` passes, and there's a "Send Next in Sequence" button for the next-due step; Steve wants a per-step Send Now action that fires THIS step immediately regardless of order, so a BDR can push out a follow-up the moment they want to.
- [ ] **Calendly/iClosed-style scheduling tool**, integrated with Google Calendar:
  - Reads BDR availability from Google Calendar
  - Configurable buffer / meeting length / windows per user
  - Public booking page (e.g. `/book/{user-slug}`) with branded layout
  - Auto-creates the event on both calendars + emails confirmation
  - Should embeddable in emails (link), appear in the contact card actions, and be the destination of post-call sequence "calendar nudge" steps
  - This replaces iClosed for our team

### Security + code-cleanup followups (from end-of-session audit, 2026-05-06)
- [x] Merge Company endpoint admin-gated (was open to all roles — fixed in same session)
- [ ] **`Set-Cookie` flags on /t/{token} redirect** — currently `secure=False, httponly=False`. The cookie is set on prospector.* and isn't actually read by anything (the bymp.com snippet sets its OWN cookie via `document.cookie='bmp_visitor='+id`). Either remove the prospector cookie entirely OR flip it to `secure=True, httponly=True` for defense-in-depth.
- [ ] **CORS posture** — `app/main.py` currently uses `allow_origins=["*"]` with `allow_credentials=True`. Browsers reject `*` + credentials per spec, so the credentials line is effectively dead, but it's a smell. Restrict to `https://prospector.backyardmarketingpros.com` + `https://backyardmarketingpros.com` and keep credentials off — we use Bearer tokens in localStorage, not cookies, so credentialed CORS is unnecessary.
- [ ] **Rate limiting on /api/track/pageview** — public, unauth'd, no rate cap. Mitigation in place: unknown `visitor_token`s create dangling rows that don't surface anywhere, but a malicious actor could fill the page_views table. Add a simple per-IP token-bucket (slowapi or homebrew). Low priority.
- [ ] **Notifications endpoint scope** — `/api/notifications/recent` returns ALL hot-lead / reply activities to every authed user. For the BMP team this is intentional (full visibility) but should eventually filter by `Company.assigned_to == user.email` for larger teams.
- [ ] **Split company_routes.py** — 1241 lines and growing. Move merge + enrich + pursue + reviews into separate modules under `app/routes/companies/`.
- [ ] **HEAD method on /track.js** — currently 405; harmless (browsers GET, not HEAD), but link-checkers / monitoring tools will alert.
- [ ] **Dormant Twilio SMS code** in `app/services/twilio_sms.py` + `/api/twilio/sms/*` endpoints. Kept on purpose (might re-enable as a fallback channel) but worth re-evaluating in a few months — if we never need it, delete.

### Compliance / hygiene
- [ ] **Send caps per domain per day** (deliverability protection — limit ~50/sender/day)
- [ ] **Lost-reason capture** on closed_lost deals (dropdown: not interested / wrong fit / went w/ competitor / no budget / no response / other)
- [ ] **Bounce auto-handling** is partially wired; ensure UI shows BOUNCED contacts clearly and prompts for alternate email

### UX polish
- [ ] **Mobile PWA** polish — currently desktop-first
- [ ] **Universal Cmd+K search** — power-user efficiency
- [ ] **CSV import** for bulk uploading existing customer data
- [ ] **Bulk actions** — mass tag, mass assign, mass enrich

### Foundation
- [ ] **README** — explain the architecture, deploy flow, how to run locally
- [ ] **Smoke tests** — at least pytest for the migration scripts and a couple of route happy-paths

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
