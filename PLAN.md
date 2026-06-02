# Multi-tenant SaaS Plan — AgencyProspector

**Status:** Draft for sign-off · **Author:** Claude (Opus 4.7) + Steve · **Date:** 2026-06-02
**Goal:** Convert the single-tenant Prospector platform that currently serves Backyard Marketing Pros into a multi-tenant SaaS sold to other BDR-driven agencies, while keeping BMP running as tenant #1 with zero downtime.

This document is the contract for the build. Phase A starts once Steve signs off.

---

## 1. Product in one line

**AgencyProspector** is a multi-tenant cold-outreach + CRM platform that agencies white-label. Each tenant brings their own Twilio, Resend, and Apollo accounts; the platform wraps proprietary enrichment, AI sequence generation, and audit services that customers can't trivially replace.

---

## 2. Architecture at a glance

| | Today (single-tenant) | Future (multi-tenant) |
|---|---|---|
| Codebase | One FastAPI app for BMP | **Same one FastAPI app** serving every tenant |
| Database | One Supabase Postgres, ~30 tables | Same DB, every tenant-owned table gets a `tenant_id` column + RLS |
| Tenant scoping | Implicit (all data is BMP) | `tenant_id` carried in JWT, enforced in app *and* in RLS |
| Reverse proxy | nginx + manual Let's Encrypt | **Caddy** with auto-SSL for any domain pointed at us |
| Domains | `prospector.backyardmarketingpros.com` | `agencyprospector.com` (platform) + `<tenant>.agencyprospector.com` (default) + customer custom domains |
| Runtime config | One singleton row | Per-tenant config row |
| API keys | Plain TEXT in DB | Encrypted at the column level (Fernet, master key in `.env`) |
| Sending | Single Resend account, single `go.bymp.com` domain | Each tenant's own Resend, own `send_domain` |
| Calls / SMS | Single Twilio account | Each tenant's own Twilio sub-account (BYO) |
| Billing | None | Stripe subscriptions + metered overages |

**Killing the duplicate tree:** the cloned-repo approach is abandoned. We go forward with this repo only.

---

## 3. Data model: `tenant_id` everywhere

Every tenant-owned table gets a non-null `tenant_id INTEGER REFERENCES tenants(id)` column, indexed. The non-exhaustive list:

`users, companies, contacts, generated_emails, campaigns, campaign_logs, campaign_targets, campaign_runs, campaign_members, deals, activities, tasks, tags, company_tags, custom_field_definitions, audit_log, api_keys, runtime_config, audit_reports, bookings, sequence_templates, notification_prefs, sms_messages, scheduled_events, ...`

Tables that are NOT tenant-scoped (global): `tenants`, `plans`, `domains` (lookup), and anything Stripe-side.

**Backfill:** every existing row gets `tenant_id=1` (BMP). One idempotent migration script + the same chained-init pattern we already use.

**Constraints:** all foreign keys within tenant data should also constrain tenant equality (e.g., a `Contact.company_id` must point to a Company in the same tenant). Enforced via composite check constraints where practical, app-layer validation otherwise.

---

## 4. Tenant resolution + routing

**Request → tenant** resolution order:

1. **Subdomain or custom domain** (Host header) — primary path. Reverse proxy reads `Host`, app middleware looks up `domains.host → tenant_id`. Sets `request.state.tenant_id`.
2. **JWT claim** — every issued token carries `tenant_id`. After login, all API calls auth via the JWT; the middleware cross-checks JWT tenant against host tenant and rejects mismatches.
3. **Platform-admin impersonation** — JWT can carry both `actor_id` (you) and `acting_as_tenant_id` (the customer). Audit-logged on every action.

**Login URLs:**
- Marketing/signup: `agencyprospector.com`
- Tenant login (default subdomain): `<tenant>.agencyprospector.com/login`
- Tenant login (custom domain): `prospector.theiragency.com/login`
- Platform admin: `admin.agencyprospector.com` (super-super-admin only)

**Cookies / JWT:** issued for the tenant's host. Cross-domain not needed since each tenant lives at one host.

---

## 5. Custom domains + SSL (Caddy)

**Default:** wildcard A record `*.agencyprospector.com → VPS IP`. New tenants get `<slug>.agencyprospector.com` instantly with no DNS work.

**Custom domain flow:**
1. Customer enters their domain in Settings: `prospector.theiragency.com`
2. Platform shows: "Add CNAME `prospector.theiragency.com → app.agencyprospector.com` and TXT `_agencyprospector-verify=<random>` to prove ownership"
3. Customer adds records; clicks "Verify"
4. We resolve the CNAME + read the TXT; on success, insert into `domains` table with `tenant_id`
5. Caddy is configured to use the on-demand TLS module that asks our app "should I provision a cert for this host?" → app says yes if it's in `domains`. Cert provisioned automatically.
6. Customer's domain is live.

**Cert ops:** zero. Caddy handles issuance + renewal. We just authorize which hostnames are permitted.

**Webhooks** stay on the canonical `app.agencyprospector.com` regardless of custom domain. Resend, Twilio, Stripe webhooks always hit the platform URL — never a tenant's white-label domain.

---

## 6. Per-tenant runtime config

`runtime_config` becomes a per-tenant table (one row per tenant). The platform brings all API keys (see §7), so this table now holds **per-tenant configuration**, not per-tenant credentials.

Per-tenant fields:
- Sending: `send_subdomain` (e.g., `go.<tenant>.agencyprospector.com`), `reply_subdomain`, `inbound_reply_subdomain` — auto-provisioned on tenant creation
- Twilio identity: assigned phone numbers, A2P brand registration ID, Trust Hub customer profile ID
- Brand: org name, postal address (CAN-SPAM footer), sender display defaults, logo URL, color palette
- Behavior: messaging direction defaults, send window, pipeline stages, notification preferences
- BYO sending domain (optional upgrade): if a tenant brings their own `go.theiragency.com`, store the DNS verification state here

**Platform credentials** (Twilio account SID, Resend API key, Anthropic key, Netrows key, etc.) live in the platform's `.env` — single set, used for every tenant via the wrapped-service accessors. All secrets in the `.env` are loaded once at boot; no plaintext stored in DB.

**App-layer encryption** still applies to any sensitive per-tenant data that does land in the DB (Twilio Trust Hub registration info, BYO domain secrets, webhook signing secrets for tenant-specific endpoints). Fernet (`cryptography` library) with a master key in `.env`.

---

## 7. API key strategy + integrations

**Decision: platform brings ALL keys.** Customers never see an API key. Everything is metered against per-plan credit bundles with hard caps (see §8). This is the GoHighLevel / Apollo / Clay model — friendliest possible onboarding + highest margin + zero runaway-bill risk for either side.

| Service | Model | Notes |
|---|---|---|
| Twilio (voice + SMS) | **Platform-controlled** | Single Twilio account; each tenant = Trust Hub Customer Profile + A2P brand. We provision phone numbers from our inventory or port the tenant's existing number. |
| Resend (email) | **Platform-controlled** | Single Resend account; each tenant gets an auto-provisioned subdomain `go.<tenant>.agencyprospector.com` registered as a separate Resend domain (independent DKIM/SPF/DMARC → independent reputation). BYO domain available as upgrade. |
| Anthropic (Claude) | **Platform-controlled** | Single Anthropic account, prompt caching + model tiering enforced (see §8). Customer's AI usage metered against bundle. |
| OpenAI | **Platform-controlled** | Same — pluggable behind the model abstraction; not used today but supported for future model choice. |
| Apollo / Hunter / ZoomInfo | **Platform-controlled** | Single keys; enrichment lookups metered against bundle. |
| Google Maps | **Platform-controlled** | Single key; place lookups bundled. |
| Netrows (enrichment) | **Platform-wrapped** ⭐ | Moat — proprietary capability. |
| DataForSEO (audits) | **Platform-wrapped** ⭐ | Moat. |
| Deepgram (transcription) | **Platform-controlled** | Call-recording transcription. |
| Blooio (iMessage) | **Platform-controlled** | iMessage send/receive. |

⭐ = the moat services — what makes the platform sticky. A customer who churns can't take Netrows + DataForSEO with them.

**Enterprise BYO escape valve** (deferred to Phase 2 — see §16): the rare sophisticated customer who insists on their own Anthropic key for compliance reasons gets it as a paid enterprise add-on. Their AI usage doesn't count against the bundle; they pay Anthropic directly. ~10% of customers max; not built in v1.

---

## 8. Credits model (everything metered, hard caps prevent runaway bills)

A `tenant_credits` table tracks usage per service per billing period. Every paid action decrements. Soft warning at 80%, hard cap at 100% — at 100%, the feature stops serving and the customer must buy a top-up pack or upgrade their plan. This is what prevents the "$5K surprise bill" problem: **we never serve usage that isn't paid for.**

**Metered services (8 total):**
- AI sequences (Claude/OpenAI generations)
- Email sends (Resend)
- Outbound call minutes (Twilio voice)
- SMS sends (Twilio)
- iMessages (Blooio)
- Enrichment lookups (Netrows + Apollo + Hunter + ZoomInfo combined)
- Audit reports (DataForSEO)
- Transcription minutes (Deepgram)

**Top-up packs** for power users who hit caps mid-period:
- 100 AI sequences for $80
- 500 AI sequences for $375 (encourages prepay → cleaner cash flow than overage billing)
- Similar packs for the other metered services

Monthly reset at the Stripe subscription anniversary. Platform admin can manually grant bonus credits per tenant (apology gestures, trial extensions).

### Unit economics safeguards (build in Phase A)

The wrap-and-mark-up model only stays profitable if we protect the AI margin. Two implementation requirements from day one:

1. **Anthropic prompt caching** — Claude charges 10% of the normal input rate for cached prompt prefixes. Our sequence-generation prompts have large stable prefixes (system instructions, business context, examples) that compress beautifully. Implementation: structure all Anthropic calls to put stable content in the cacheable prefix, dynamic content (this contact's data) at the tail. **Expected cost reduction: ~80% on input tokens, ~4x margin improvement per sequence.**

2. **Model tiering per task** — not every AI call needs Sonnet. Use Claude Haiku (~5x cheaper) for: skip-condition evaluation, lead scoring, quick personalization touches. Reserve Sonnet for the actual sequence-generation prompts where output quality is the product. Currently we use Sonnet for everything — fixable.

Both are ~1 day of refactor each. Built in Phase A so the unit economics are healthy from the day we charge a customer.

---

## 9. Pricing tiers (starting numbers — tune after 60 days)

Per-seat pricing. Bundles include the most realistic per-seat-per-month usage; power users buy top-up packs (§8) or upgrade tier.

| Plan | Per seat/mo | AI sequences | Emails | Call min | SMS | iMsg | Enrich | Audits | Transcript |
|---|---|---|---|---|---|---|---|---|---|
| **Starter** | $197 | 150 | 1,000 | 300 | 100 | 200 | 500 | 5 | 5h |
| **Growth** | $297 | 500 | 3,000 | 1,000 | 500 | 1,000 | 2,000 | 25 | 25h |
| **Scale** | $497 | 1,500 | 6,000 | 3,000 | 1,500 | 3,500 | 6,000 | 100 | 100h |
| **Enterprise** | custom | custom | custom | custom | custom | custom | custom | custom | custom |

**Overage rates** (when a customer hits cap and buys overage rather than upgrading):
- AI sequence: **$1.00 each** (cost ~$0.012 with caching → ~80x markup, plenty of room for promo discounts)
- Email send: **$0.005**
- Call minute: **$0.05**
- SMS send: **$0.04**
- iMessage: **$0.10**
- Enrichment: **$0.20**
- Audit: **$2.00**
- Transcript minute: **$0.30**

**Top-up packs** (cheaper per-unit than overage to encourage prepay):
- 100 AI sequences: **$80** ($0.80 each)
- 500 AI sequences: **$375** ($0.75 each)
- Similar volume discounts on other services

**Annual discount:** 20% (paid up-front).

**First customer (friendly trial):** treat as paid Growth tier with the first 90 days free. They get the full real experience with no billing risk; we get production-realistic feedback that will retune these numbers.

---

## 10. Platform admin console (the "GHL-like" layer)

Lives at `admin.agencyprospector.com`. Only the platform owner (super-super-admin role) sees it.

**Must-have at launch:**
- Tenants list (name, plan, MRR, # users, last activity, % bundle used, status)
- **"Sign in as tenant"** — impersonation. Confirmation modal requires typed reason. JWT carries `actor_id` + `acting_as_tenant_id`. Red banner stays on screen ("You are signed in as Agency X"). Every action while impersonating is audit-logged.
- Suspend / restore tenant (suspended = sequence engine halts for them; UI shows billing-on-hold wall)
- Create new tenant + invite first super_admin

**Within first 30 days:**
- Per-tenant usage dashboard (credits burned, trend, who's near cap)
- Billing panel (Stripe customer, current period usage, next invoice estimate, manual credit grants)
- Platform-wide metrics (MRR, churn, signups, top tenants by usage)
- Cross-tenant audit log (every impersonation + admin action, immutable)

**Later:**
- White-label theming controls per tenant
- Plan upgrade/downgrade workflow with proration
- Feature flags per tenant

---

## 11. Onboarding flow

Radically simpler than the BYO-keys version. Customer never enters an API key — platform owns everything. Goal: tenant signs up and is sending email within **5 minutes**, calling within **10 minutes**, SMS-ready in 2-3 weeks (A2P delay is unavoidable).

When a new tenant signs up (or platform admin creates one):

1. **Create tenant + first super_admin user**, send invite email
2. New super_admin lands at `<tenant>.agencyprospector.com/onboard`
3. **Step 1: Brand** — agency name, logo upload, brand colors, postal address (CAN-SPAM footer), default sender display name
4. **Step 2: Pick a phone number** — choose by area code from our Twilio inventory (instantly assigned), OR port an existing number (10-business-day process). Voice calling is **live immediately** with a fresh number.
5. **Step 3: Sending email — auto-provisioned** — platform spins up `go.<tenant>.agencyprospector.com` as a new Resend domain in our account. SPF/DKIM/DMARC auto-configured (we control the parent DNS). **Live in 60 seconds.** Optional: upgrade later to BYO domain.
6. **Step 4: A2P 10DLC registration** — collect EIN, legal business name, sample message use case, opt-in language source. We submit Trust Hub registration on the tenant's behalf. **SMS goes live 2-3 weeks later** when carriers approve; UI clearly states the timeline.
7. **Step 5: Invite team** — add seats (each = a user with a role)
8. **Step 6: Confirm plan** — Starter / Growth / Scale; trial period applies if friendly
9. Done → land on dashboard, ready to prospect.

**No API keys collected.** No DNS work for the customer. The only thing that takes time is A2P 10DLC carrier approval, which is universal across every platform doing business SMS.

Onboarding state is persisted; the customer can resume from where they left off. Steps 4-6 are individually skippable; only Brand + Phone number are required to enter the app.

---

## 12. Phase-by-phase task list

### Phase A — Foundation (2 weeks) — **SUBSTANTIALLY COMPLETE 2026-06-02**
**Goal:** code is multi-tenant-safe + AI unit economics are healthy. No behavior change visible to BMP users.

- [x] Create `tenants` table; insert tenant #1 (BMP) — `2d9a8f6`
- [x] Migration: add `tenant_id` to every tenant-owned table (32 tables), backfill to 1 via DEFAULT, NOT NULL enforced — `2d9a8f6`
- [x] `Tenant` model + `TenantMixin` applied to 30 mapped classes + 2 association tables — `36d8c26`
- [x] `tenant_domains` table seeded with BMP's 3 hosts → tenant 1 — `69ee6c1` / `e57b69b`
- [x] Tenant resolver `app/tenancy.py` (JWT claim → custom domain → `{slug}.agencyprospector.com` → tenant 1 fallback) — `69ee6c1`
- [x] Postgres RLS `tenant_isolation` policy on all 32 tables (dormant; activates when we migrate off the BYPASSRLS `postgres` role) — `60480f8`
- [x] `get_tenant_db` FastAPI dependency that stamps `session.info["tenant_id"]` + sets the GUC — `f3b5bce`
- [x] ORM auto-tenant-filter via `do_orm_execute` hook (real enforcement today) — `f726ec4`
- [x] INSERT auto-stamp via `before_flush` hook (`tenant_id` set automatically) — `2ccad94`
- [x] 215 routes across 30 route files migrated to `get_tenant_db` — `1be7ad0`, `68d602a`, `9927d72`, `0e5acd3`
- [x] JWT carries `tenant_id` claim minted at login; resolver checks claim first — `0e5acd3`
- [x] Background sequence engine + activation/wake/morning-brief passes iterate per active tenant, scoped via `tenant_scope` — `f0d1879`
- [x] Encrypted per-tenant secrets vault (`tenant_secrets` table + Fernet, `app/secrets_vault.py`) — `e5a9340`
- [x] AI client wrapper (`app/services/ai_client.py`) — model tier constants + prompt-cache helper — `3de11ca`
- [x] Reply classifier moved to Haiku 4.5 + prompt-cached system rubric (~5x cheaper) — `3de11ca`
- [x] Cold-email generator prompt-cached on its ~1500-token composed system prompt (5-10x cheaper after first prospect) — `3de11ca`
- [ ] Per-tenant `runtime_config` accessor refactor (defer: today's accessors work tenant-scoped via ORM filter; per-tenant override columns + helpers come with first non-BMP onboard)
- [ ] Sweep remaining 7 email_generator callsites onto `ai_client.chat_with_system` + cacheable=True
- [ ] Smoke tests: prove tenant A can't see tenant B's via API or direct DB queries (manual probe done with tid=99 returning 0 rows; formal test fixture pending)

### Phase B — Tenant routing, onboarding, admin console (1.5 weeks)
**Goal:** you can create agency #2 yourself in 20 minutes.

- [ ] Switch reverse proxy: install Caddy, port nginx config, on-demand TLS hook to our app
- [ ] `domains` table + `Host → tenant` middleware
- [ ] Custom domain verification flow (CNAME + TXT)
- [ ] Tenant creation API + UI
- [ ] Platform admin console (tenants list, suspend, impersonate)
- [ ] Impersonation: JWT with `acting_as_tenant_id` + red banner + audit log
- [ ] Onboarding wizard (steps 1-9 above)
- [ ] Sending-domain DNS verification helper

### Phase B½ — Credits ledger (1 week)
**Goal:** every paid service is metered and hard-capped.

- [ ] `tenant_credits` table with per-service counters and reset_at
- [ ] Decrement hooks at every metered call site: **AI sequences, emails (Resend), call minutes (Twilio voice), SMS (Twilio), iMessages (Blooio), enrichments (Netrows + Apollo + Hunter + ZoomInfo), audits (DataForSEO), transcription (Deepgram)** — 8 services total
- [ ] Soft warning at 80% + hard cap at 100% with platform-admin bypass flag
- [ ] Top-up pack purchase flow (Stripe checkout → credit grant)
- [ ] Per-tenant usage view in platform admin (current period, trend, top consumers)
- [ ] Customer-facing usage dashboard (their bundle, what's burned, what's left, top-up CTA)
- [ ] Monthly auto-reset on Stripe billing anniversary

### Phase C — Billing (1 week)
**Goal:** customers can self-serve subscribe + upgrade.

- [ ] Stripe customer + subscription objects per tenant
- [ ] Plans defined in code (Starter / Growth / Scale), tier metadata
- [ ] Per-seat pricing math (subscription quantity = active users)
- [ ] Metered usage records → Stripe usage records for overages
- [ ] Billing UI: current plan, invoices, payment method, upgrade/downgrade
- [ ] Trial logic (90 days free for friendly #1)
- [ ] Dunning: hard-cap when payment fails

### Phase D — Polish & launch (1 week)
**Goal:** ship it.

- [ ] White-label theming controls (logo + 2 color picks per tenant)
- [ ] Cross-tenant isolation test suite (automated, must pass before any deploy)
- [ ] Marketing site at `agencyprospector.com` (one-pager → signup)
- [ ] Customer-facing docs (onboarding, integrations, billing)
- [ ] Internal support process (Linear/Email/Slack-with-customer pattern)
- [ ] Migrate BMP officially to tenant #1 (sanity check; should be a no-op)
- [ ] Onboard agency #2

**Total: ~6 weeks focused.**

---

## 13. Migration plan (BMP → tenant #1)

Zero-downtime. Phases A-D each deploy independently.

1. Phase A backfill: every BMP row gets `tenant_id=1`. The app code is updated to read `tenant_id` from the JWT (defaulting to 1 if missing, with a deprecation warning) and write `tenant_id=1` on new rows. BMP continues running. **No user-visible change.**
2. Phase B brings Caddy + subdomain routing online. `prospector.backyardmarketingpros.com` becomes a custom domain attached to tenant #1. BMP keeps logging in at their existing URL. **No user-visible change.**
3. Phase C activates Stripe. BMP gets a placeholder $0 subscription as tenant #1; we'll decide later whether to bill ourselves.
4. Phase D: marketing site goes live at `agencyprospector.com`. Agency #2 signs up.

**Rollback plan:** every phase is a git revert + redeploy. Migrations are additive (we never drop the old single-tenant columns until Phase D + a 30-day cooldown).

---

## 14. Security model

- **Defense in depth:** app-layer scoping + DB-layer RLS. A bug in one layer doesn't leak data.
- **RLS policies:** `tenant_id = current_setting('app.tenant_id')::int` on every tenant table. The Postgres session variable is set by middleware at request start.
- **Encryption at rest:** API keys/secrets encrypted at the column level. Disk-level encryption (Supabase default) is not enough — a backup leak would still expose keys.
- **Impersonation audit log:** every "sign in as" action recorded with actor, tenant, reason, IP, timestamp. Immutable table.
- **Secret rotation:** master Fernet key rotation procedure documented; supports a key versioning scheme.
- **Webhook signatures:** every inbound webhook (Resend, Twilio, Stripe, Blooio, iClosed) signature-verified before processing.

---

## 15. Decisions made (the contract)

| # | Decision | Date |
|---|---|---|
| 1 | One codebase, multi-tenant. Kill the duplicate tree. | 2026-06-02 |
| 2 | Hybrid domain model: default subdomain + optional custom domain via CNAME. | 2026-06-02 |
| 3 | Caddy replaces nginx for auto-SSL on arbitrary hostnames. | 2026-06-02 |
| 4 | **Platform brings ALL keys** (Twilio, Resend, Anthropic, OpenAI, Apollo, Hunter, ZoomInfo, Google Maps, Netrows, DataForSEO, Deepgram, Blooio). Customers never see an API key. | 2026-06-02 |
| 5 | Every paid service is metered with **hard caps** (no runaway bills). Top-up packs and tier upgrades are how power users get more. | 2026-06-02 |
| 5a | **Anthropic prompt caching + Claude model tiering** (Sonnet for generation, Haiku for lighter tasks) built into Phase A. Protects AI margin from day one. | 2026-06-02 |
| 6 | Per-seat tiered pricing ($197 / $297 / $497) with bundled usage across 8 metered services. Numbers tuned after 60d real-usage data. | 2026-06-02 |
| 7 | Tenant resolution: Host header primary, JWT claim secondary, impersonation via combined claims. | 2026-06-02 |
| 8 | First customer = friendly trial, treated like a paid Growth-tier customer with 90-day free period. | 2026-06-02 |
| 9 | Marketing/platform admin at `agencyprospector.com`; tenants at `<slug>.agencyprospector.com` or custom domains. | 2026-06-02 |
| 10 | **Sending: auto-provisioned subdomain** `go.<tenant>.agencyprospector.com` on tenant creation. BYO sending domain available as upgrade (path BMP will take). | 2026-06-02 |
| 11 | **SMS: A2P 10DLC registration per tenant via Twilio Trust Hub**. Voice live immediately, SMS live 2-3 weeks after tenant signup. | 2026-06-02 |

---

## 16. Deferred (Phase 2 of SaaS, not now)

- White-label support portal (each tenant gets their own help docs)
- Tenant-level feature flags (turn off iMessage for one customer)
- SSO (Google / Microsoft / SAML)
- Multi-region deployments
- Read replicas for analytics
- **Enterprise BYO API keys** (Anthropic, Twilio, Resend) for the rare customer with compliance/volume reasons to want their own. Sold as a paid enterprise add-on with the customer's usage exempt from the bundle. Build when the first customer asks; ~80% of customers will never want this.
- Tenant data export (GDPR self-serve)
- Multi-currency billing (USD only for v1)

---

## 17. Open business items (parallel to engineering, don't block Phase A)

- **Legal:** ToS, Privacy Policy, DPA template, MSA template for enterprise
- **Insurance:** E&O for SaaS — required before taking any paying customer
- **Branding:** logo, marketing site copy, screenshots, demo video
- **First customer:** identified? Outreach started?
- **Support tooling:** Help Scout / Front / Intercom / just email + Notion docs?
- **Domain:** `agencyprospector.com` purchased + DNS configured? Wildcard `*.agencyprospector.com` pointed at VPS?
- **Twilio Trust Hub ISV account:** since we'll register every customer as a Trust Hub Customer Profile + A2P brand, we need our own ISV-tier account set up with Twilio. Apply early — takes ~1 week. Determines whether SMS will be ready by Phase D.
- **Anthropic volume / Build tier:** at scale we can negotiate Anthropic pricing down further (pure margin). Worth a conversation once we have ~10 paying customers' usage data.
- **Stripe Connect or Stripe SaaS billing setup:** which model? Subscription + metered usage is the v1 plan; configure now so it's ready for Phase C.

---

## 18. What signs us off into Phase A

This document, with explicit OK on:
1. The 9 decisions in §15
2. The phasing in §12
3. The pricing in §9 (as starting numbers, not final)

Once signed, Phase A kicks off. First commit: the `tenants` table + `tenant_id` column on every table + the backfill migration.

— end of plan —
