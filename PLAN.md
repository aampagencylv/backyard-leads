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

`runtime_config` becomes a per-tenant table (one row per tenant). Refactor `get_twilio_credentials(db)`, `get_netrows_api_key(db)`, etc. to take `tenant_id` and pull the per-tenant row.

Per-tenant fields:
- BYO API keys (Twilio account SID + auth token + API key/secret, Resend API key, Anthropic optional, Apollo, Hunter, ZoomInfo, Google Maps)
- Sending: `send_domain`, `reply_domain`, `inbound_reply_domain`
- Brand: org name, postal address, sender display defaults, logo URL, color palette
- Behavior: messaging direction defaults, send window, pipeline stages
- Webhook secrets (Resend, Blooio, iClosed) — per-tenant

**Encryption (block 4):** every secret field (API keys, signing secrets) is encrypted at write time with Fernet (`cryptography` library) using a master key in `.env`. The accessor functions transparently decrypt. Reads return the plaintext for use; the DB never holds plaintext.

---

## 7. API key strategy + integrations

| Service | Model | Notes |
|---|---|---|
| Twilio (voice + SMS) | **BYO required** | Customer owns numbers + A2P + caller-ID reputation |
| Resend (email) | **BYO required** | Customer's sending domain + reputation |
| Anthropic (Claude) | **BYO + platform fallback** | Platform-provided as paid add-on for the BYO-averse |
| Apollo / Hunter / ZoomInfo | **BYO required** | Often already owned |
| Google Maps | **BYO required** | Free tier handles most |
| Netrows (enrichment) | **Platform-wrapped** ⭐ | Moat — never exposed |
| DataForSEO (audits) | **Platform-wrapped** ⭐ | Moat — never exposed |
| Deepgram (transcription) | **Platform-wrapped** | Small cost, bundled |
| Blooio (iMessage) | **Platform-wrapped** | No customer accounts exist |

⭐ = the platform-wrapped services are what the credits ledger tracks.

---

## 8. Credits model (platform-wrapped services only)

A `tenant_credits` table tracks usage per service per billing period. Decrement on each call. Soft warning at 80%, hard cap at 100% (configurable, can be disabled per-tenant by platform admin).

Metered services: **enrichment lookups · audit reports · iMessages · transcription minutes**. (Customer's own Twilio/Resend/Anthropic spend is invisible to us.)

Monthly reset at the Stripe subscription anniversary.

---

## 9. Pricing tiers (starting numbers — tune after 60 days)

| Plan | Per seat / mo | Enrichments | Audits | iMessages | Transcript hrs |
|---|---|---|---|---|---|
| **Starter** | $197 | 500 | 5 | 200 | 5 |
| **Growth** | $297 | 2,000 | 25 | 1,000 | 25 |
| **Scale** | $497 | 6,000 | 100 | 3,500 | 100 |
| **Enterprise** | custom | custom | custom | custom | custom |

**Overages:** `$0.20/enrichment · $2/audit · $0.10/iMessage · $0.30/transcript minute`

**Annual discount:** 20% (paid up-front).

**First customer (friendly trial):** treat as paid Growth tier with the first 90 days free. They get the full real experience with no billing risk; we get production-realistic feedback.

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

When platform admin creates a new tenant (or self-service signup later):

1. **Create tenant + first super_admin user**, send invite email
2. New super_admin lands at `<tenant>.agencyprospector.com/onboard`
3. **Step 1: Brand** — agency name, logo upload, postal address (CAN-SPAM footer)
4. **Step 2: Connect Twilio** — paste account SID + auth token + buy/assign a phone number
5. **Step 3: Connect Resend + sending domain** — paste API key, configure `go.theiragency.com` DNS (SPF/DKIM/DMARC), wait for verification
6. **Step 4: Connect Anthropic** (or skip → use platform with markup)
7. **Step 5: Connect enrichment services** (Apollo / Hunter / ZoomInfo — any or all)
8. **Step 6: Invite team** — add seats
9. **Step 7: Set plan** — confirm trial/paid plan
10. Done → land on dashboard

Each step is skippable except brand/Twilio/Resend (you can't do anything useful without sending + calling). Resume from where you left off.

---

## 12. Phase-by-phase task list

### Phase A — Foundation (2 weeks)
**Goal:** code is multi-tenant-safe but no behavior change visible to BMP users.

- [ ] Create `tenants` table; insert tenant #1 (BMP)
- [ ] Migration: add `tenant_id` to every tenant-owned table, backfill to 1, add NOT NULL constraint
- [ ] Tenant context middleware (resolves from Host header / JWT claim)
- [ ] Refactor `scope_companies` and every other query helper to scope by tenant
- [ ] Per-tenant `runtime_config` table; refactor accessors (`get_twilio_credentials`, etc.) to take `tenant_id`
- [ ] App-layer Fernet encryption for all API-key columns; master key in `.env`
- [ ] Supabase RLS policies: every tenant table gets a policy `tenant_id = current_setting('app.tenant_id')`
- [ ] Smoke tests: prove tenant A can't see tenant B's data via the API or direct DB queries

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

### Phase B½ — Credits ledger (3-4 days)
**Goal:** wrapped-service usage is metered and enforced.

- [ ] `tenant_credits` table with per-service counters and reset_at
- [ ] Decrement hooks at every wrapped-service call site (Netrows, DataForSEO, Blooio, Deepgram)
- [ ] Soft warning (80%) + hard cap (100%) with bypass flag
- [ ] Per-tenant usage view in platform admin
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
| 4 | BYO API keys for Twilio, Resend, Apollo, Hunter, ZoomInfo, Google Maps, Anthropic (with platform fallback). | 2026-06-02 |
| 5 | Platform-wrapped (and credit-metered): Netrows, DataForSEO, Deepgram, Blooio. | 2026-06-02 |
| 6 | Per-seat tiered pricing ($197 / $297 / $497) + overages on the 4 wrapped services. Numbers tuned after 60d real-usage data. | 2026-06-02 |
| 7 | Tenant resolution: Host header primary, JWT claim secondary, impersonation via combined claims. | 2026-06-02 |
| 8 | First customer = friendly trial, treated like a paid Growth-tier customer with 90-day free period. | 2026-06-02 |
| 9 | Marketing/platform admin at `agencyprospector.com`; tenants at `<slug>.agencyprospector.com` or custom domains. | 2026-06-02 |

---

## 16. Deferred (Phase 2 of SaaS, not now)

- White-label support portal (each tenant gets their own help docs)
- Tenant-level feature flags (turn off iMessage for one customer)
- SSO (Google / Microsoft / SAML)
- Multi-region deployments
- Read replicas for analytics
- AI cost flow-through with markup (revisit once an enterprise customer asks)
- Tenant data export (GDPR self-serve)

---

## 17. Open business items (parallel to engineering, don't block Phase A)

- **Legal:** ToS, Privacy Policy, DPA template, MSA template for enterprise
- **Insurance:** E&O for SaaS — required before taking any paying customer
- **Branding:** logo, marketing site copy, screenshots, demo video
- **First customer:** identified? Outreach started?
- **Support tooling:** Help Scout / Front / Intercom / just email + Notion docs?
- **Domain:** `agencyprospector.com` purchased + DNS configured?

---

## 18. What signs us off into Phase A

This document, with explicit OK on:
1. The 9 decisions in §15
2. The phasing in §12
3. The pricing in §9 (as starting numbers, not final)

Once signed, Phase A kicks off. First commit: the `tenants` table + `tenant_id` column on every table + the backfill migration.

— end of plan —
