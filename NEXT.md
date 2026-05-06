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

### Twilio Integration (future)
- Call from platform (click-to-call on contact card)
- Text messaging as a sequence step type (already modeled)
- Call recording + transcription → auto-log to timeline
- SMS sequence steps auto-send via Twilio

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
