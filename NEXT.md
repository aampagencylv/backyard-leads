# Next Steps & Punch List

> Living doc. Pick up here at the start of each session. Pull `git pull --ff-only origin main` first.
> Last updated 2026-05-08 (post-audit cleanup — many "backlog" items were already shipped).

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
| 1 | ✅ **Netrows Starter** subscribed | Done 2026-05-12 |
| 2 | ✅ **Rotated `pk_live_*` Netrows key** | Done 2026-05-12 |
| 3 | ✅ **BMP postal address** set in `.env` (`Backyard Marketing Pros · 4375 S Valley View Blvd Ste G · Las Vegas NV 89103`) | Done 2026-05-12 |
| 4 | ✅ **`RESEND_WEBHOOK_SECRET`** set in VPS `.env` | Done 2026-05-12 |
| 5 | ✅ **`ICLOSED_WEBHOOK_SECRET`** set | Done 2026-05-12 |
| 6 | ✅ **Reps have `twilio_phone_number` assigned** | Done 2026-05-12 |
| 7 | ✅ **Google OAuth** set up | Done 2026-05-12 |

### Google OAuth — full setup (one-time, ~10 min)

Required only once at the platform level. After this, each rep just clicks "Connect Google Calendar" in Settings.

**1. Configure the OAuth consent screen** (Google Cloud Console)
- Go to [console.cloud.google.com](https://console.cloud.google.com) → pick or create a project (e.g. "BMP Prospector")
- Left nav → **APIs & Services** → **OAuth consent screen**
- User type: **External**
- Fill in:
  - App name: `BMP Prospector` (or whatever you want users to see on the consent screen)
  - User support email: yours
  - App logo: optional (BMP logo recommended)
  - Authorized domains: `backyardmarketingpros.com`
  - Developer contact email: yours
- Save → **Add or Remove Scopes** → add these four:
  - `openid`
  - `.../auth/userinfo.email`
  - `.../auth/calendar.readonly`
  - `.../auth/calendar.events`
- Save. (You can leave it in "Testing" mode — Google will show a warning screen to users until you have 100+ users and submit for verification. For BMP's internal team that's fine indefinitely.)

**2. Enable the Google Calendar API**
- APIs & Services → **Library** → search "Google Calendar API" → **Enable**

**3. Create OAuth Client credentials**
- APIs & Services → **Credentials** → **+ Create credentials** → **OAuth client ID**
- Application type: **Web application**
- Name: `BMP Prospector Web`
- **Authorized JavaScript origins**: `https://prospector.backyardmarketingpros.com`
- **Authorized redirect URIs**: `https://prospector.backyardmarketingpros.com/api/google/oauth/callback` *(this is the only URL the OAuth callback ever uses — no others needed)*
- Create → copy the **Client ID** + **Client secret** that appear

**4. Drop into VPS env + restart**
```bash
ssh vps "echo 'GOOGLE_OAUTH_CLIENT_ID=<paste-client-id>' >> /opt/backyard-leads/.env"
ssh vps "echo 'GOOGLE_OAUTH_CLIENT_SECRET=<paste-client-secret>' >> /opt/backyard-leads/.env"
ssh vps "systemctl restart backyard-leads"
```

**5. Connect each rep**
- Each rep opens **Settings → Google Calendar** → clicks **🔌 Connect Google Calendar**
- Google's consent screen shows the four scopes; rep approves
- They'll see "⚠️ Google hasn't verified this app" — click **Advanced → Go to BMP Prospector (unsafe)**. Normal for unverified internal apps; harmless once you trust it
- After redirect they see a green "connected" badge with their email
- Below appears the **⏰ Booking Availability** panel — set weekly hours, slot length, etc.

**6. Test**
- Settings → Booking Availability → click **👁️ Preview slots (next 7 days)** → confirm slots render
- Open the booking URL shown in Settings (e.g. `https://prospector.backyardmarketingpros.com/book/steven-edwards`) in an incognito tab
- Pick a slot, fill in test name/email, hit Confirm → you should see:
  - The Google Calendar event on the BMP Discovery Calls calendar
  - A `meeting_booked` Activity on any matched company timeline
  - A confirmation email in the prospect's inbox (via Resend) plus Google's own invite

---

## 🟢 Backlog — ranked by ROI

> **Audit cleanup 2026-05-08:** the queue below was found to overstate open
> work by ~6 sections. Removed: items confirmed shipped via code audit
> (call recording + transcription + waveform, lead scorer v2, sequence-
> engine call steps + skip-if logic, website visitor tracking, iClosed
> gated competitor report, conditional-sequence logic, custom fields,
> auto-competitor-comparison generator). Git history preserves the
> original design notes for each.

### 🔥 Inbox capture (Phase A SHIPPED, Phase B locked for next)

**A. Token-based reply catching** — SHIPPED. Reply-To rewriting, Resend
Inbound webhook → `/api/email/inbound`, signature mining, auto-enrich,
sequence auto-pause. Awaits operator setup (see Action items above).

**B. Missive sidebar app** — locked for next session. Hosted at
`/missive-sidebar`, iframe-embedded. Matches company/contact on From
address; "Add to CRM" if not found, full card + actions if found.

**Phase A operator setup** (only thing remaining for inbound replies):
1. In Resend dashboard, add an `email.received` webhook for
   `go.backyardmarketingpros.com` → POST to
   `https://prospector.backyardmarketingpros.com/api/email/inbound`. Save
   the signing secret Resend generates.
2. `ssh vps "echo 'RESEND_WEBHOOK_SECRET=<secret>' >> /opt/backyard-leads/.env && systemctl restart backyard-leads"`
   (without this the webhook accepts any payload).
3. Smoke test: reply to a test sequence email, watch for `email_replied`
   Activity within ~30 sec, sequence auto-pause, forwarded copy in Missive.

**Phase C — Full Missive send integration (deferred indefinitely).** Park
unless Resend becomes a constraint.

### Twilio HubSpot Calling replacement — Phase 5 only (Phases 1-4 + 6 SHIPPED)

Phases 1-4 (per-rep numbers, browser dialer, recording + Deepgram
transcription + Claude summary, inbound routing with voicemail) are live.
Phase 6 (Blooio iMessage) shipped 2026-05-06.

**Phase 5 — reporting + power dialer (1-2 days, OPEN):**
- Calls per rep per day chart on dashboard
- Connect rate = connected / dialed
- Average talk time + outcome funnel (dialed → connected → demo-booked → closed)
- Power dialer mode: feed saved view to dialer, auto-advance on call-end,
  one-click log + dial-next. Human-initiated only (TCPA).

Compliance still applies: 2-party consent disclosure (already wired in
TwiML), DNC list check before dialing (TODO before power dialer ships),
call-hours enforcement, TCPA — power dialer must stay human-initiated.

**Operator note:** if you're not seeing a waveform on a call, check that
the rep has `twilio_phone_number` assigned on their User record — TwiML
refuses to record without a verified caller ID.

### ✅ Verified shipped (audit 2026-05-08)

These were all listed as open in the prior backlog but a code audit
confirmed they're live. Each retains its design history in git.

- **Sequence engine — call steps + conditional skip logic** —
  `DEFAULT_SEQUENCE` in `app/services/sequence_engine.py` includes
  email/linkedin/call/imessage step types with `skip_if` populated;
  engine evaluates conditions at runtime.
- **Website visitor tracking** — `/t/{token}` redirect, `/track.js`,
  `/api/track/pageview`, install snippet box in Settings. Outbound
  URLs in sequence emails are auto-wrapped.
- **iClosed gated competitor report** — full state machine on
  `/report/{token}/compare` and `/report/{token}/competitors`;
  background generator runs SERP audit + side-by-side comparison;
  auto-emails when ready; webhook flips `booked_at`.
- **Conditional sequence logic** — `skip_if_json` populated at
  generation, evaluated at execute time.
- **Custom fields (companies + contacts)** — hybrid model: dedicated
  columns for socials/revenue + JSON blob for tenant-defined fields,
  with API stable keys for Zapier.
- **Automated competitor comparison report** —
  `_generate_competitor_report_bg` audits the top 3 SERP competitors
  via DataForSEO and renders branded comparison HTML.
- **Lead scoring v2 (fit × intent)** — `app/services/lead_scorer.py`
  with sentiment-weighted intent + decay; wired into dashboard Hot
  Leads.

### Other high-value items
- [ ] **Dashboard MRR/ARR cards** — wire forecast API to dashboard KPI strip
- [ ] **Saved views UI** — dropdown on Companies + Pipeline pages (API ready)

### Tier 2 Netrows — ✅ DONE (audit 2026-05-12)
- [x] `/businesses/search` (Yellow Pages) — shipped 2026-05-12, `/api/search/yellow-pages` endpoint with domain-dedupe + Search history row
- [x] `/yelp/business-search` + `business-details` + `business-reviews` — wrapped + wired via `/api/companies/{id}/refresh-yelp` + rendered on company detail
- [x] `/similarweb/website-overview` — wrapped + wired into the regular enrich flow with 30-day TTL + UI panel
- [x] `/technographics/lookup` (BuiltWith) — wrapped + wired into enrich flow + UI tech-stack panel
- [x] `/indeed/job-search` — wrapped + dedicated `/api/companies/{id}/refresh-indeed` endpoint
- [x] `/companies/by-domain` (LinkedIn) — wrapped inside `enrich_company_by_domain`, pulls employee count + founded year as part of the standard enrich step

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

### 🔥 ACTIVE QUEUE (locked 2026-05-08, post-audit)
1. **Tier 2 Netrows endpoints** — IN PROGRESS this session. ~3 hr.
2. **Calendly-style native scheduler + Google OAuth** — built together
   since OAuth is required for calendar reads. Native scheduler design
   already lives in [Native scheduler](#-saas-feature-native-scheduler-calendly-style-byo-google-calendar)
   below; OAuth scope covers (a) Gmail send for users who prefer it
   over Resend, (b) calendar availability reads, (c) Sign-in-with-Google
   for user auth.
   - **Audit page logo upload follow-up:** the generic
     `POST /api/uploads/logo` endpoint shipped with the booking-page
     branding work. When we touch the audit reports next, swap the
     hardcoded BMP logo for a per-org uploaded one using the same
     endpoint + a settings UI mirror of Calendar's "Look & feel" panel.
3. **Missive sidebar app (Inbox capture Phase B)** — hosted at
   `/missive-sidebar`, iframe-embedded, matches company/contact on
   From address. "Add to CRM" if not found, full card + actions if
   found. Auth via shared secret + Missive's iframe-postMessage.
4. **Twilio Phase 5** — power dialer + per-rep call reporting.
5. **AI chatbot widget** — design in [§ AI chatbot](#-ai-chatbot-ask-bmp--conversational-query-widget) below.
6. **Tier 3 Netrows Radar** — deferred; not needed for current customers.
7. **Blooio iMessage SaaS add-on** — slots in once the billing layer
   exists; design in [§ Blooio iMessage](#-saas-add-on-blooio-imessage-locked-2026-05-08) below.

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

## 📅 SaaS feature: Native scheduler (Calendly-style, BYO Google Calendar)

Steve's idea while we shipped iClosed: many future SaaS tenants won't
already have iClosed (or won't want to pay for Calendly). The platform
should ship with a native scheduling option — pick a time slot, drop on
the user's Google Calendar, send a confirmation email — so they can
self-serve without a third-party subscription.

**Inspiration / reference (NOT a direct adoption — just for shape):**
https://github.com/stefanodecillis/slotty — open-source Calendly clone
in TS/React + a small Node API, MIT licensed. Worth reading for:
  - The slot generation algorithm (recurring availability rules → free
    slots within a window, accounting for existing calendar events)
  - The booking confirmation flow (event creation, email send, ICS attach)
  - The public booking page UX

**Architecture for OUR build:**

We don't need Slotty's UI directly — we already have a brand-styled
gate page (the iClosed embed) that we can swap to a native calendar
when the tenant chooses "Native Scheduler" instead of "iClosed". Core
pieces we'd own:

1. **Google OAuth flow**
   - Add Google as the second auth provider (after email/password)
   - Scopes: `calendar.readonly`, `calendar.events` (write our own events
     under a dedicated "BMP Bookings" calendar so we don't pollute the
     user's primary calendar)
   - Refresh-token storage on User: `google_refresh_token`,
     `google_calendar_id` (the dedicated calendar's id)
   - Token rotation handled server-side; user never sees raw tokens

2. **Per-user availability config**
   - New `scheduling_config` table: user_id, timezone, default_slot_minutes
     (15/30/45/60), buffer_before_minutes, buffer_after_minutes,
     min_lead_time_hours, max_advance_days, daily_limit
   - Recurring availability rules: weekday → list of (start_time, end_time)
     spans (e.g. Mon-Fri 9am-5pm with a 12-1 break)
   - Date-specific overrides: hold-out dates, vacation, special hours
   - "What we charge for the call" — optional event title prefix /
     description template

3. **Public booking endpoint**
   - GET /book/{user_slug}?slot_minutes=30 → branded page with calendar
     grid, available slots derived from (availability rules) - (existing
     Google Calendar events) - (already-booked slots in our DB)
   - Slot generation runs server-side per-request; no caching beyond
     5 min (calendars change fast)
   - Form: name + email + phone + custom message
   - Submit → POST /book/{user_slug}/confirm → creates Google Calendar
     event + sends ICS-attached confirmation email + creates Activity
     in CRM (mirrors the iClosed webhook flow)

4. **Per-tenant scheduler choice**
   - New `Settings.scheduler_choice` field: 'iclosed' | 'native_google'
     | 'cal_com' (future) | 'none'
   - When 'native_google': the Settings UI shows the Google OAuth
     connect button + availability config form
   - When 'iclosed': the existing iClosed booking URL field stays
   - The competitor-report gate page picks the right embed at render
     time based on tenant's choice

5. **Calendar sync direction**
   - **Read** the user's primary calendar to find busy/free
   - **Write** booked events to a dedicated "BMP Discovery Calls"
     calendar (auto-created on first connect) so:
     - The user can see all bookings in one place
     - Disconnecting the integration doesn't delete their personal events
     - We can rebuild state from our DB if Google disconnects

**Cost model (SaaS):**
  - Google Calendar API is free (high quota — 1M reads/day per user)
  - Email send goes through Resend (already metered)
  - No per-booking fee on our side
  - Tenant wins by avoiding Calendly's $12/user/mo

**Compliance:**
  - GDPR: invitee email captured on the booking form, store with
    explicit "I agree to be contacted" checkbox
  - Google OAuth verification — eventually need to go through the
    formal review process for `calendar.events` scope when we hit
    100+ tenants. Pre-verification works fine for early SaaS users
    (Google shows a warning screen; users click through)

**Where this slots in priority:**
  - After SoS scrapers, after billing layer, but before public Beta
  - Multi-week build (~2-3 weeks for production-quality):
    - Week 1: Google OAuth + token storage + dedicated calendar create
    - Week 2: Availability rules engine + slot generation
    - Week 3: Public booking page + confirmation flow + tenant
      scheduler-choice toggle

**Open questions to resolve before starting:**
  - Do we want Cal.com as a third option? (open-source, self-hosted
    or BYO-key — could be the "power user" tier)
  - Round-robin team scheduling (multi-host events) — defer to v2
  - Webhook to other CRMs when a booking happens — fits naturally
    with our future Zapier integration

---

## 🏛️ Enrichment Phase 2: Secretary of State adapters (locked 2026-05-08)

Steve's idea while shipping the enrichment waterfall: SoS data is a
high-margin, vertical-relevant enrichment source. Public-record by
nature, free to access, and it surfaces information the other providers
either don't have or have stale versions of:

  - Registered legal name (vs. DBA / Google Maps name)
  - Registered agent name + address (often the actual owner / their attorney)
  - Officers / directors / managing members
  - Business age (filing date)
  - Active / dissolved / inactive status (don't waste outreach on a dead LLC)
  - Cross-LLC linkage — same owner often has multiple entities; consolidate to one prospect

Why it matters for BMP's verticals: home-services owners are typically
the registered agent OR named officer. Apollo / Netrows miss them because
they're not on LinkedIn. SoS catches that gap.

### Architecture fit
The waterfall I'm building today is class-based — every provider
implements the EnrichmentProvider protocol. Each US state becomes its
own provider class:

```
ApolloProvider           (BYO-key, tenant pays)
NetrowsProvider          (platform-paid)
HunterProvider           (platform-paid)
SoSProvider_AZ           (platform-paid; light scrape compute)
SoSProvider_NV           (...)
SoSProvider_FL           (...)
SoSProvider_TX           (...)
SoSProvider_CA           (...)
```

The waterfall picks the right SoS provider by `company.state` so we
only hit one per lookup. A small `SOS_REGISTRY` dict maps state → provider.

### State priority for v1
Pick states by BMP's customer concentration:

| State | Difficulty | Notes |
|---|---|---|
| **AZ** | Easy | eCorp searchable, scrape-friendly. BMP's home market. |
| **NV** | Easy | SilverFlume; Vegas territory. |
| **FL** | Easy-medium | Sunbiz.org — best public dataset of any US state. Officers + addresses exposed. Huge backyard-pro market. |
| **TX** | Hard | SOSDirect requires login + per-search fee. May need to skip in v1 or use aggregator. |
| **CA** | Hard | Bizfile JS-heavy + rate-limited. Defer. |
| **OK** | Easy | Free public search. Smaller market, but easy win. |
| **WA** | Easy | CCFS public search, scrape-friendly. |
| **NY** | Easy | Bulk data download available. Less relevant for BMP but high-value for SaaS expansion. |

v1 ship: AZ + NV + FL — covers BMP's territory + biggest backyard market.

### Compliance notes
- SoS records are public — no GDPR/CCPA per se
- Each state's ToS varies; some explicitly disallow scraping for
  commercial use. Rate-limit to 1 req/sec per state, identify the user-agent
  honestly, cache aggressively (records change rarely)
- Aggregator alternative: OpenCorporates ($400+/mo), Cobalt API ($0.20/lookup
  in v2 we evaluated). Worth re-pricing once we have ≥10 SaaS tenants — at
  that scale aggregator cost beats engineering + maintenance of 10+ scrapers.

### Cost / pricing
- Scrape compute: negligible (~$0.001/lookup including failed retries)
- Cache hit ratio expected high (records change slowly; cache 30 days)
- Suggested credit price: 3 credits per SoS lookup. ~$0.015 retail at ~50% margin
  over compute, OR if we end up using an aggregator we can pass through with
  a small fee (5 credits at ~$0.03 retail vs. $0.20 vendor cost)

### Build order (when we get to Phase 2)
1. Add a `state` field surfaced through the waterfall provider input
   (extend `enrich(domain, company_name, state)` signature)
2. Build `SoSProvider_FL` first — Sunbiz is the cleanest dataset and our
   biggest market outside AZ
3. AZ + NV next — BMP's territory
4. Cache layer: new `sos_lookups` table keyed by (state, company_name) with
   30-day TTL. Saves the cost on repeat enrichment of the same company.
5. UI: surface SoS fields in the company detail panel (registered agent,
   officers, filing status) — these are high-signal personalization angles
   for cold outreach ("I see you registered Smith Pools LLC in 2018, how's
   it going?")

This stays in the queue behind Phase 1 (waterfall + Apollo) — no point
adding more providers to the cascade until the cascade itself is shipped.

---

## 💬 SaaS add-on: Blooio iMessage (locked 2026-05-08)

**Pricing model**: Blooio numbers cost **$250/mo flat** per number. No
per-message charge. We're NOT including iMessage in the base SaaS plan —
it's a paid add-on customers opt into.

**What needs to exist:**
1. **Tenant add-ons concept** — first real add-on the platform sells. Likely a
   `tenant_addons` table or a JSON column on the (future) `organizations` model:
   `{"imessage": {"active": true, "blooio_number": "+1...", "since": "..."}}`.
   This is also the right shape for AI Voice, extra sender domains, etc. — design once.
2. **Settings → "Plan & Add-Ons" page** (admin-tier, not super_admin) — shows
   active add-ons + available upgrades. "Add iMessage — $250/mo" CTA → Stripe
   checkout → webhook flips the add-on flag → number provisioned.
3. **Capability gating** — every UI surface that exposes iMessage (contact cards,
   compose modals, SMS-vs-iMessage choice) should hide or replace those controls
   with an "Upgrade to enable iMessage" prompt when the add-on isn't active.
   Server-side enforcement on `/api/blooio/*` routes — no relying on the UI.
4. **Number provisioning** — open question, see below.

**Open question — does Blooio's API provision numbers programmatically?**
We currently use Blooio's `/v2/api/chats/{id}/messages` for sends and
`/v2/api/phone-numbers/lookup` for capability checks. Whether they expose a
"create / claim / assign number" endpoint isn't something I've verified. Two
possibilities:
- **Yes** → end-to-end self-serve: Stripe payment → API call → number live, all
  inside our flow.
- **No** → semi-manual: Stripe payment → CRM task surfaces to Steve → he
  provisions in Blooio dashboard → pastes the number into our admin UI →
  customer's iMessage goes live an hour later. Fine for v1; not great for
  scale beyond ~20 customers.

I can poke at Blooio's API docs in 10 min when you want me to — say the word
and I'll get a definitive answer. Otherwise we slot the discovery work in
right before the SaaS billing layer goes in.

**Dependencies:**
- A real `Organization` / `Tenant` model (still single-tenant today)
- Stripe billing integration with subscription + metered add-ons
- Decision on whether iMessage stays a single shared Blooio account with
  per-tenant numbers, or if each tenant gets their own Blooio account

**Where this slots in priority:**
After enrichment waterfall + Apollo adapter (the foundational SaaS architecture
work) and before God Mode / Morning brief / AI chatbot. Roughly when we start
building the real billing layer — at which point we'll be wiring Stripe and
this add-on becomes the second SKU after the base seat price.

---

## ✅ Shipped 2026-05-12 (late session — Yellow Pages + inbound notifications + cleanup)

1. **`/api/search/yellow-pages`** — Yellow Pages search wired into a real route. Domain-deduped insert into companies as `status='new'`, paginated (1–3 pages, ~30 results/page). Closes out the Tier 2 Netrows list.
2. **Inbound call browser notification** — when a Twilio.Device inbound rings and the tab isn't focused, fires a desktop notification with caller name/company. Click focuses the tab. Auto-closes when the call ends. Requests `Notification.permission` on first dialer init (one-time prompt).
3. **Floating help button overlap fixed** — `feedback-btn` was at `left: 20px` which overlapped the 240px sidebar's user-info section. Moved to `left: 260px` on desktop with a mobile media-query fallback to `left: 20px`.
4. **Postal address + all operator setup items resolved** — `BMP_POSTAL_ADDRESS`, `RESEND_WEBHOOK_SECRET`, `ICLOSED_WEBHOOK_SECRET`, Google OAuth, Twilio phone assignments all completed by Steve.

## ✅ Shipped 2026-05-11 — 2026-05-12 (Call coaching, autopilot, team dashboard, polish)

Long multi-zone session. Order shipped (~16 commits, `bb0404a..` to `99ceb0d..`):

### Call recording & coaching
1. **Inline waveform on every call row** — replaced the `▶️ Play` toggle button with an always-visible player on the dashboard recent-calls widget + company timeline. Solved the silent-failure bug where the proxy required Bearer auth but `<audio>` elements can't attach headers — now we mint a short-lived signed token (30-min TTL, scoped to activity_id) appended as `?t=…`.
2. **Tokenized recording proxy** — `/api/twilio/recording/{id}?t=<jwt>` plus the helper `mint_recording_token` / `verify_recording_token` in `app/auth.py`. The dashboard + company timeline serializers bake the URL into the API response so the canvas / `<audio>` just uses it.
3. **Recording proxy ownership check** — security audit found that bearer-auth path accepted any valid JWT regardless of role/ownership. Now enforces: admin = all access; sales_rep = only activities on their assigned companies (falls back to `user_id` match on the activity).
4. **Transcript auto-poll for fresh calls** — for calls < 5min old with a recording but no transcript yet, the page polls every 20s for up to 3 min so Deepgram's output lands without a manual refresh.
5. **Multichannel transcription** — Deepgram switched from `diarize=true` (voice-based) to `multichannel=true`. Twilio already records `record-from-answer-dual` (channel 0 = rep, channel 1 = prospect), so per-channel decoding is now near-perfect instead of voice-fingerprint guesswork.
6. **Diarization persistence** — new columns `activities.diarized_segments_json` (`[{speaker, start, end, text}, …]`) + `talk_ratio_json` (`{rep_words, prospect_words, rep_pct, prospect_pct, single_speaker}`). Both used to be transient. Backfill script `scripts/backfill_diarization.py` (null transcripts, re-run pipeline).
7. **CallRail-style dual-channel canvas waveform** — custom canvas renderer drawing agent bars (dark blue) above center, customer bars (light blue) below, with played portion in darker shades. Web Audio API decodes the bytes client-side into ~220 amplitude buckets, speaker resolved per-column from diarized segments. Click anywhere to scrub. Native `<audio>` drives playback. Falls back to the iMessage-style single-track wavesurfer for legacy un-diarized calls.
8. **Over-talking coaching indicator** — when `rep_pct > 60%` (industry guideline: rep should listen ~55%), player border turns orange, agent chip turns orange + bold, "⚠️ Over-talking — coach to listen more" pill appears.
9. **Single-speaker recordings handled** — voicemails / dropped calls / muted sides come back as 100/0 from Deepgram. We now flag `single_speaker: true` on the talk_ratio JSON and the UI renders "🎙️ Single-speaker recording" instead of the misleading 0% / 100% chips. Over-talking flag suppressed for these.

### Pipeline editor (tenant-configurable middle stages)
10. **`/api/pipeline/config` GET + PUT** — stages stored as JSON blob on `runtime_config.pipeline_stages_json`. System stages (`in_sequence` / `closed_won` / `closed_lost` / `snoozed`) stay fixed in code; only middle stages (default: `qualified` → `proposal` → `negotiation`) are editable.
11. **Settings → Pipeline Stages editor** (admin only) — system stages shown locked, middle stages get drag-handle ▲/▼ + color picker + name + probability % + delete. Adding a stage rotates default colors. Save migrates any deals on dropped stages to the first surviving middle stage so nothing strands.
12. **Pipeline kanban renders dynamically** — `data.stage_meta` from `/api/pipeline` includes color + name + system flag; cards inherit the column color on the left border. Existing 15 deals on "prospecting" migrated to "qualified" via one-shot SQL.
13. **Snooze-wake / engagement promotion rewired** — when a sequence engagement signal (3+ opens, click, audit booking) fires, deal moves from `in_sequence` to the *first configured middle stage* instead of the old hardcoded "prospecting". Snooze restore falls back to `in_sequence` if the stage was deleted while asleep.
14. **Pipeline rep-filter dropdown** — admins get a dropdown ("All reps" / per-user) on the pipeline view; reps see only their own deals automatically via existing `scope_deals`.
15. **Audit log** for pipeline + autopilot config writes — both fire `record_audit` so we have history of org-wide setting changes.

### Autopilot send window v2 (per-channel + basis radio)
16. **`/api/autopilot/send-window` GET + PUT** — RuntimeConfig grew `autopilot_basis` (contact / rep / strictest) + per-channel hours (email + iMessage) + per-channel weekday JSON + `respect_rep_presence` (stub flag).
17. **`app/services/send_window.py`** — owns config read, contact-TZ inference (phone area code → company state → rep TZ → LA), per-channel window check, strictest-of-both math (hour-by-hour walk, 192 max iterations), `snap_to_window`, `snap_pending_steps_to_window`.
18. **Settings → Sequence Autopilot panel** — basis radio with explainers ("Strictest of both" recommended), per-channel cards with hour pickers + weekday pills, dimmed "Respect rep online presence" checkbox (coming soon), live "🔍 Try it on a real contact" preview widget.
19. **Sequence engine + every generation site uses the window** — engine `_maybe_defer_for_send_window` now takes `channel`, runs through the service, defers steps outside window. Major creation sites (contact pursue, company pursue v2, campaign batch, post-call sequence, manual rework, 30-day) all call `snap_pending_steps_to_window` so the UI never shows midnight queueings.

### Booking routing (BDR → host calendar)
20. **`User.default_booking_host_id`** + **`SchedulingConfig.conflict_calendar_ids_json`** — schema columns + migration.
21. **`app/services/booking_host.py`** — `resolve_booking_host(db, user)` returns the user who should own this user's bookings (defaults to themselves; falls back to themselves if host is inactive / hasn't connected Google). `resolve_booking_url(db, user)` returns the `/book/<slug>` URL.
22. **Email signature uses resolver** — render_signature now substitutes the host's slug, so BDR signatures route discovery-call bookings to the admin's calendar.
23. **`/api/me/scheduling/book-for-contact` validates + writes to host's calendar** — slot validation, free-busy, Google event create, Booking row all use the host. BDR added as co-attendee. Activity log says "booked by <BDR>".
24. **`/api/me/scheduling/preview?effective=true`** — in-app Schedule Meeting modal calls with `effective=true` so BDRs see the host's available slots (not their empty own calendar).
25. **Multi-calendar conflict check** — `fetch_user_busy` unions primary + write-target + any IDs in `conflict_calendar_ids_json`. Personal/family calendars now block slots even though we never write to them.
26. **`/api/me/scheduling/my-google-calendars`** — lists user's Google calendars for the multi-select picker. Calendar Settings UI gains a "Block off conflicts from other calendars" section with checkboxes (primary + write-target always on, others togglable).
27. **Admin → Users → "Books on" column** — dropdown: Own calendar / any team member with Google connected. PATCH `default_booking_host_id` on the user.

### Sequence panel polish (the "I don't see the + sign" / "I don't see resume" round)
28. **Sequence body expanded by default** on the company-detail view — per-step "+ Add Step" pills between cards are now visible without clicking Expand.
29. **Summary-row "+ Add Step"** alongside Pause + Restart — also surfaces on the dedicated `renderSequencePanel` editor view (Steve's screenshot showed this was missing).
30. **"⚠️ Stalled" badge + "▶ Restart from today" button** when unsent steps have `scheduled_send_at` in the past but no `paused_at`. `resume_sequence` extended to handle the stalled case — re-anchors unsent steps to today + snaps them into the autopilot window.

### Team Overview dashboard (manager view)
31. **`/api/dashboard/team`** — one aggregation endpoint behind admin gate. Returns 7 zones: KPI strip (calls/emails/meetings today + this-week + WoW deltas, won-MTD), BDR leaderboard (per-rep table with calls/emails/iMessages today, meetings this week, open pipeline $, open deal count, health flag badges ⏰🐢🎙️, last-active timestamp), coaching watchlist (last-7d calls with rep_pct > 60 OR unrated), stuck-pipeline-by-BDR (deals untouched >14d grouped by owner), reply-sentiment-per-BDR (stacked horizontal bars by sentiment bucket), 14-day activity heatmap (rows = BDRs, cols = days, color tint = volume), conversion funnel per BDR (sequences → opens → replies → meetings → won + reply→meeting %).
32. **Tab strip on dashboard** — "Team Overview" / "My Activity". Admin defaults to Team. Reps don't see the tabs at all.
33. **Single-prefetch of company → owner mapping** — was N+1 lookups, now one query before the aggregation loops run.

### Other UI polish
34. **Voice-note style inline waveform** for legacy un-diarized calls — red circular play button, dark bars centered on midline, time on the right.
35. **Sidebar logo block white background** — the dark green logo against the dark green nav looked muddy. Now sits on a clean white field while the nav stays green underneath.
36. **Kanban scroll fix** — `.main-content` needed `min-width: 0` so the wide kanban scrolled within its container instead of expanding past the viewport and dragging the top-bar over the sidebar.
37. **Mobile pass for Team Overview** — leaderboard + funnel + heatmap tables horizontal-scroll with `min-width` floors; inner 2-col grid collapses to single column at <900px; KPI strip goes to 2 columns.

### Security audit fixes
- Recording proxy ownership check (see #3 above)
- Autopilot preview scope check — `/api/autopilot/preview?contact_id=X` rejects requests for contacts the requesting rep doesn't own (admins still see all)
- Audit log entries for `pipeline_stages.updated` + `autopilot_send_window.updated` (see #15)

### Migrations chained on startup this session
- `migrate_pipeline_stages.py` — runtime_config.pipeline_stages_json
- `migrate_autopilot_window.py` — runtime_config.autopilot_send_start_hour / end_hour / days_json
- `migrate_autopilot_per_channel.py` — basis + per-channel hours + days + presence flag (with backfill of legacy autopilot_send_* into the email row)
- `migrate_booking_routing.py` — users.default_booking_host_id + scheduling_configs.conflict_calendar_ids_json
- `migrate_call_diarization.py` — activities.diarized_segments_json + talk_ratio_json

### Deferred to next time
- **Smoke tests** for `pipeline_config`, `send_window`, `booking_host` — services have nontrivial math (strictest-of-both, snap-to-window edge cases) and zero tests. ~1 hr.
- **Rep-presence heartbeat** — `respect_rep_presence` field is wired but the actual heartbeat needs PWA push notifications + a `/api/me/heartbeat` endpoint that the installed PWA pings every 2 min. Then engine reads the timestamp and gates sends accordingly. Probably 2-3 hrs once we want to ship presence-based gating.

### Action items waiting on Steve
- **Pick a "Books on" host for each BDR** in Settings → Team Members once you onboard reps (currently 1 active rep with Google connected — yourself)
- **Visual smoke test** on a real connected call (the two existing test calls came back 100% one-speaker because they were one-sided audio; need a real two-way call to confirm the dual-channel viz looks like CallRail's screenshot)

---

## ✅ Shipped 2026-05-10 — 2026-05-11 (Missive sidebar + Chrome extension + PWA)

Massive late-evening session. Order shipped:

### Subdomain split (SaaS-ready surface architecture)
1. **`audit.backyardmarketingpros.com`** subdomain — all `/report/{token}` URLs in cold emails + SMS + audit CTAs route through this. CNAME-ready for white-label tenants.
2. **`schedule.backyardmarketingpros.com`** subdomain — native scheduler `/book/{slug}` lives here. Same FastAPI backend serves all three subdomains via Nginx server_name routing.
3. Per-surface CSS-variable + Nginx vhosts + Let's Encrypt certs done for both.
4. Memory file `project_backyard_leads_subdomains.md` documents the three-surface split.

### Email deliverability stack
5. **DNS Health monitor** (`🩺 DNS Health` in sidebar, super-admin only) — runs DoH lookups against Google Public DNS for SPF / DKIM / DMARC / MX / open-pixel host + the three subdomain A records. Per-check OK/WARN/ERROR + overall pill.
6. **Resend webhook hardened** — Svix signature verification now enforced (was a TODO). Added `delivered_at` / `opened_at` / `open_count` / `bounced_at` / `complained_at` columns on `GeneratedEmail`. Migration backfills delivered_at from sent_at on existing rows.
7. **Deliverability dashboard** (`📈 Deliverability`, super-admin only) — overall + per-mailbox bounce/complaint/open rates, 7-day daily stacked-bar trend, recent offenders. Auto-sync of Missive labels on email.bounced / email.complained / email.replied / email.opened / email.clicked.
8. **Plain-text alternative** on every outbound Resend send (Beautifulsoup-based html_to_plain_text with link preservation).
9. **Pre-send spam score** (`app/services/spam_score.py`) — heuristic logged on every send, also exposed as `POST /api/admin/reputation/spam-check` for composer-side preview.

### Org-branded email signature
10. New `brand_website_url` field on RuntimeConfig + Org Brand panel input. Signature template now pulls colors / logo / company name / website from RuntimeConfig. `render_signature` is async; all 7 call sites updated.

### Audit report polish
11. CTAs renamed `"Schedule a Discovery Call"` → `"Schedule A Discovery Call"` everywhere; competitor report gains a top-section CTA + bottom CTA both wired to `audit_scheduler_type` (iclosed/native/custom).

### Missive sidebar integration (v1 → v2.9, full feature parity)
12. **v1: shell** — `/integrations/missive/sidebar` iframe loaded by Missive when a thread is open. Auth via `Missive.initiateCallback()` → `/integrations/missive/auth` form-encoded login → JWT stashed via `Missive.storeSet`. SecurityHeadersMiddleware exempts `/integrations/missive/*` so the iframe can embed.
13. **v2: server-side Missive client** (`app/services/missive_client.py`) — wraps `GET /v1/users`, `GET /v1/shared_labels`, `POST /v1/posts` with 5-min in-process cache + a `sync_status_label()` helper. PAT auth.
14. **v2: sender heuristic fix** — team-emails list (cached from /v1/users) filters out BDR addresses when picking the prospect from a thread.
15. **v2: status → Missive label write-back** (manual button + auto on Resend webhook events). New `contacts.missive_conversation_id` + `seen_at` columns persist the thread-to-contact mapping.
16. **v2.5: full action surface** — Call (deep-link to dialer) / iMessage (inline form, Blooio) / Schedule meeting (deep-link to `openScheduleModal`) / Add task (inline form with due-date pills) / LinkedIn / Copy email / Status quick-change dropdown / Inline note save / Save as call / Pinned notes / Open tasks (with complete checkbox) / Other contacts at company / Recent sequence steps / Activity timeline.
17. **v2.6 → v2.8: button polish + inline forms** replacing `Missive.openForm` (which was unreliable). In-iframe toast system replacing `Missive.alert`. Color-scheme: light to prevent dark-mode breakage. Deep-link tokens for cross-tab SSO (Schedule meeting / Call in new tab).
18. **v2.9: iMessage inline form** — same pattern as Add Task, fully reliable.

### Vendor-name cloak
19. Swept user-visible strings to neutral terms across frontend + backend: Blooio → "iMessage service" / Netrows → "data enrichment" / Hunter → "email finder" / Resend → "email service" / Anthropic+Claude → "AI" / SimilarWeb → "traffic insights". Apollo kept (intentional BYO). Code identifiers + env vars + comments unchanged.

### Chrome extension (Manifest v3)
20. **`/integrations/embed/sidebar`** — generic, SDK-free version of the Missive sidebar; auth via URL `?t=<jwt>`, context switches via `postMessage({type:'set_email'})` or `set_linkedin`.
21. **`chrome-extension/`** scaffolded — manifest, background service worker (JWT in chrome.storage.local + popup⇄tabs broadcast), popup with login form, content scripts for Gmail + LinkedIn. Auth-expiry → red `!` badge → popup re-login → auto-clears.
22. **v2 polish** — Gmail panel only shows on open thread / compose (not inbox list). `document.body.style.marginRight` shifts Gmail content so they sit side-by-side. Reveal-tab on right edge restores collapsed panel. Latest expanded-message From parsing (was "any email on page"). LinkedIn: profile-URL → `Contact.linkedin_url` fuzzy match. Quick-add with email input when probe is a LinkedIn URL.
23. **Install panel in Settings → Integrations** — version badge + Download button (streams the zipped folder via `/integrations/extension/download`, rebuilt per request from the on-disk folder so it tracks main) + collapsible 6-step install instructions.

### PWA
24. **v1: installable** — webmanifest at `/manifest.webmanifest`, service worker at `/sw.js` (scope: `/`), 5 generated PNG icons (192/512/maskable/180/32), iOS Safari PWA meta block, `beforeinstallprompt` install pill, `appinstalled` cleanup. iOS Add-to-Home-Screen friendly.
25. **v2: mobile-responsive** — comprehensive `@media (max-width: 900px)` block. Sidebar becomes slide-in drawer with hamburger + backdrop. All multi-column grids collapse to single column. Modals go full-screen with `safe-area-inset` padding for iOS notch + home indicator. Pipeline kanban → horizontal scroll-snap. Inputs forced to 16px font (no iOS zoom). 44px+ tap targets per Apple HIG. AI chat launcher floats above home indicator.
26. **SW controllerchange auto-reload** — installed PWAs reload once when a new SW takes over so v2.x updates land without manual cache clear.
27. **Contacts page mobile polish** — card header stacks vertically, dot-separated meta becomes tappable chips, action buttons 2-up grid. Same treatment to Companies + Pipeline action rows.
28. **Deliverability dashboard locked to super-admin** (was admin+) — matches DNS Health, prevents platform-internal metrics leaking to tenant admins.

### Migrations added (idempotent, auto-chained in init_db)
- `migrate_email_events.py` — adds delivered/opened/open_count/bounced/complained timestamps + backfill
- `migrate_brand_website.py` — adds runtime_config.brand_website_url
- `migrate_missive_link.py` — adds contacts.missive_conversation_id (indexed)

### Action items waiting on Steve

1. **Create 6 Missive shared labels** matching `STATUS_TO_LABEL_NAME` (`Qualified Lead`, `Replied`, `Converted`, `Not Interested`, `In Sequence`, `Contacted`) — or tell me different names to use. Without them, tag-sync silently no-ops.
2. **Decide on Chrome Web Store submission timing** — code is multi-tenant-ready *except* `APP_URL` is hardcoded. ~3 hours to add a first-run "what's your Prospector URL?" screen + branded icons + privacy policy. Mostly tied to SaaS launch readiness.
3. **Upload proper PWA icons** if you want branded ones — current ones are a flat green "P" placeholder. Drop replacement PNGs into `static/pwa/` (192 / 512 / maskable / 180 / 32).
4. **Test the Missive sidebar end-to-end** — open Linda's thread, try every action button (Call, iMessage, Schedule meeting, Add task, status dropdown, Sync label). Sidebar v2.9 should have no SDK-popup breakage.
5. **Side-load + test the Chrome extension** — Settings → Integrations → Chrome extension → 📥 Download → drag into `chrome://extensions`. Test in Gmail (open a thread, try every action) and LinkedIn (visit any `/in/<slug>` profile).

### Next session priorities (in rough order)

1. **More mobile polish per page** — Steve called out Contacts (done), but Tasks / Calendar / Pipeline detail / Audit Reports settings page / DNS Health probably need similar treatment.
2. **Email deliverability follow-ups still pending** —
    - Apply org brand to sequence email **bodies** (not just signatures — the message HTML container is still hardcoded BMP styling)
    - Pre-send spam score UI in the composer (backend endpoint exists; just needs a "🛡️ Check" button + inline issue display in the email editor)
    - "Send Next Step Now" execute_step_now path is wired but worth integration-testing end-to-end
3. **Postgres migration** (Phase 4, deferred to SaaS milestone) — still the right move pre-multi-tenant. Plumbing the database_url change + an Alembic migration of the SQLite schema is ~2 days; the actual data move on prod is ~1 hour.
4. **Docker + CI smoke tests** — still pending. A Dockerfile + a minimal GitHub Actions workflow (`pytest -q` + smoke-curl prod after deploy) prevents the "I shipped a broken endpoint and didn't notice" failure mode.
5. **Web Push notifications** — biggest single PWA-quality upgrade. Notify BDR of hot replies / new HOT leads via the home-screen icon's badge even when the app is closed. ~1 day; gives the team a real reason to keep the PWA installed.
6. **Capacitor wrapper** — when ready for App Store / Play Store presence. ~2-3 weeks total including review.

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
