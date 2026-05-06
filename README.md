# Backyard Leads — Prospector + CRM

Internal tool for Backyard Marketing Pros: scrape Google Maps for home-services
prospects, enrich with website analysis + decision-maker email lookup, generate
personalized AI cold-email sequences, send via Resend, and manage the full sales
pipeline through deal close.

Live at: <https://prospector.backyardmarketingpros.com>

---

## Architecture

- **Backend:** FastAPI + SQLAlchemy 2.0 (async) + aiosqlite
- **Frontend:** Single `static/index.html` (vanilla JS, no build step). Fully server-rendered shell + client-side fetch.
- **DB:** SQLite at `/opt/backyard-leads/leads.db` (gitignored). Single-file portability; backups are just file copies.
- **AI:** Anthropic Claude (Sonnet 4) for cold + follow-up email generation.
- **Email:** Resend (SMTP-as-a-service) — outbound + open/click webhooks.
- **Enrichment:**
  - **Netrows** (`api.netrows.com`) — verified decision-maker emails (the load-bearing one)
  - **Hunter** — fallback for generic emails + email pattern detection
  - **Apollo** — kept for legacy reasons; near-zero hit rate on SMB segment
- **Hosting:** systemd service on a Hostinger VPS, behind nginx + Let's Encrypt.

## Repository layout

```
app/
  main.py                  # FastAPI factory + router registration
  config.py                # pydantic-settings, .env-driven defaults
  database.py              # async engine + Base
  auth.py                  # JWT + bcrypt
  runtime_config.py        # DB-backed key/value config (overrides env)
  models.py                # User, Company, Contact, Deal, GeneratedEmail,
                           # Activity, Tag, Task, RuntimeConfig
  routes/
    auth_routes.py         # /api/auth/* — register, login, /me
    search_routes.py       # /api/search/* — Google Maps prospect scrape
    company_routes.py      # /api/companies/* — list, detail, enrich, pursue
    contact_routes.py      # /api/contacts/* — CRUD + per-contact sequences,
                           # reverse-lookup, email-finder, post refresh
    deal_routes.py         # /api/deals/*, /api/pipeline, /api/forecast
    send_routes.py         # /api/send/* — outbound + Resend webhook,
                           # auto-pause on reply, auto-task on engagement
    crm_routes.py          # /api/crm/* — tags, tasks, activity timeline, search
    dashboard_routes.py    # /api/dashboard, /api/activity/feed
    runtime_routes.py      # /api/runtime-config — Settings → API Keys
    unsubscribe_routes.py  # /unsubscribe?t=token (public, CAN-SPAM)
  services/
    map_scraper.py         # Google Maps Places API
    website_intel.py       # Crawl + analyze problems on prospect websites
    local_seo_intel.py     # Local-SEO scoring
    apollo_enrichment.py   # Apollo (effectively dead for SMB)
    hunter_enrichment.py   # Hunter (Free plan respects 10-result cap)
    netrows_enrichment.py  # Netrows decision-maker, reverse-lookup,
                           # maps reviews, LinkedIn posts, email-finder
    email_generator.py     # Anthropic Claude prompts
    email_sender.py        # Resend HTTP API + List-Unsubscribe headers
    signature.py           # Render the standardized BMP signature
  templates/
    email_signature.html   # Jinja2 template, fixed across all users
static/
  index.html               # The whole frontend
scripts/
  deploy.sh                # ./scripts/deploy.sh — pulls main, restarts service
  migrate_signature_fields.py
  migrate_leads_to_companies.py
  migrate_netrows_caches.py
  migrate_runtime_config.py
docs/
  netrows-openapi.json     # 273 Netrows endpoints, saved for offline reference
NEXT.md                    # Living punch list. Read first thing each session.
deploy.sh                  # VPS bootstrap (initial install)
```

## Database migrations

All migrations are **idempotent** and run automatically as systemd
`ExecStartPre` steps on every restart, in this order:

1. `migrate_signature_fields.py` — consolidated user name/title/phone/signature → first_name/last_name/nickname/phone_number/scheduling_url
2. `migrate_leads_to_companies.py` — split Lead into Company/Contact/Deal entities
3. `migrate_netrows_caches.py` — added `companies.reviews_json`, `contacts.recent_posts_json`
4. `migrate_runtime_config.py` — created singleton `runtime_config` table

To add a new migration: drop a `migrate_*.py` script in `scripts/`, append its
`ExecStartPre=` line to `/etc/systemd/system/backyard-leads.service`, and
`systemctl daemon-reload`.

## Working across two machines

Steve works from home and the office on separate Macs, both running Claude Code.
**GitHub `main` is the source of truth.** Standard flow:

```bash
# At session start (always do this first)
cd ~/projects/backyard-leads
git pull --ff-only origin main
cat NEXT.md            # see what's open

# At session end (push your work)
git add . && git commit -m "..." && git push origin main

# To deploy to prod from anywhere
./scripts/deploy.sh
```

`scripts/deploy.sh` SSHes into the VPS (host alias `vps` in
`~/.ssh/config`), pulls main, pip-installs, restarts the systemd service
(which runs all migrations via ExecStartPre), and pings `/health`. It also
parse-checks `static/index.html`'s embedded JS before deploying so a typo
can't make it to prod.

## Running locally

```bash
git clone git@github.com:aampagencylv/backyard-leads.git
cd backyard-leads
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in API keys
python run.py         # starts uvicorn on :8000 with auto-reload
```

## Backups

- **Code:** GitHub `main` is the truth.
- **DB + .env on VPS:** Daily cron at 03:00 UTC writes
  `/root/backups/backyard-leads-YYYYMMDD-HHMMSS.tar.gz`. Keeps last 14 days,
  prunes older. Uses SQLite's online `.backup` so the snapshot is
  consistent even mid-write. Script: `/usr/local/bin/backup-backyard-leads.sh`.
- **Restore:** `tar xzf /root/backups/backyard-leads-*.tar.gz -C /opt/backyard-leads/ && systemctl restart backyard-leads`.

## Operational notes

- **HTTPS:** nginx on `prospector.backyardmarketingpros.com` proxies to
  `127.0.0.1:8000`. Certificates via Let's Encrypt. Static cache is 1 day;
  HTML is no-store (so deploys are visible immediately).
- **Email signature:** the template lives at `app/templates/email_signature.html`.
  All users get the same template; only first_name, last_name, nickname,
  phone_number, scheduling_url, and email are interpolated per user.
- **CAN-SPAM compliance:** outbound emails do NOT have a visible
  unsubscribe link in the body (hurts deliverability). The
  `List-Unsubscribe` HTTP header is set on every send so Gmail/Outlook
  render their native button at top of email. Postal address is in the
  footer. The `/unsubscribe?t=token` endpoint handles header clicks.
- **Engagement signals:** Resend's `email.opened` and `email.clicked`
  events arrive at `/api/send/webhook/resend` and become Activity rows.
  3+ opens or any click auto-creates a follow-up Task (deduped against
  any open follow-up task created in the last 3 days).
- **Auto-pause on reply:** synthetic `email.replied` event handler is
  wired but requires a Gmail forwarding rule to fire (one-time setup).

## Where the bodies are buried

- **Async SQLAlchemy can't lazy-load relationships** mid-request. Always
  query through `select()` explicitly. Accessing `company.tags` or
  `contact.emails` directly without eager loading throws `MissingGreenlet`.
- **Hunter Free plan caps domain-search at 10 results.** `hunter_enrichment.py`
  auto-retries with limit=10 on a 400 pagination_error.
- **Netrows credits are per-account, not per-key.** Rotating doesn't refund.
- **`/people/search` filtering doesn't work** on Netrows — every query
  returns ~1.27 BILLION total results regardless of `currentCompany` /
  `companyId` params. Use `/email-finder/decision-maker` instead.

## Memory

The Claude Code agent maintains an auto-loaded memory file at
`~/.claude/projects/-Users-stevenedwards/memory/project_backyard_leads.md`
on each laptop. It captures the workflow, deploy commands, and pointers
to this repo. **For per-session state, read `NEXT.md` first.**
