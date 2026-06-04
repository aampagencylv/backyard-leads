# Staging Environment Setup

> **Why**: Texas Remodel Team incident (2026-06-03) — 21 days of silent bad
> sends to prod because there was no staging gate. This document is the
> one-time setup so that every future change can land on staging first,
> sit there for ~24h, and only reach BDRs after a smoke check passes.

## Architecture

Three options, ordered cheapest → most isolated. **My recommendation: option B.**

### A. Same VPS, second instance ($0/mo)

Run `staging.prospector.leadprospector.ai` on the existing VPS as a
second systemd service + a second Postgres database + Caddy entry on a
different port. Cheap but: if Postgres or the VPS itself has an issue,
staging and prod both go down. NOT recommended.

### B. Separate Hostinger VPS, same provider (~$8/mo) ⭐ RECOMMENDED

Provision a second Hostinger VPS (the smallest tier — `KVM 1` or similar,
1 vCPU + 4GB RAM is plenty for staging). Separate IP, separate Postgres,
separate Caddy. If staging breaks, prod is fully isolated.

- **Cost**: ~$8/mo
- **Isolation**: full — separate hardware, separate DB, separate Caddy
- **Setup time**: ~30 min to provision + ~1h for me to configure

### C. Render / Railway / Fly.io ($5–15/mo)

Modern container PaaS. Fastest setup but means moving away from the
VPS+Caddy pattern you've standardized on. Probably not worth the
infrastructure divergence right now.

## What I need from you (~10 minutes)

If you go with option **B**, do these three things and tell me when done:

1. **Provision a new Hostinger VPS**
   - Smallest tier (KVM 1 or equivalent)
   - Ubuntu 22.04 LTS
   - Region: same as prod (US East) to keep latency consistent
   - SSH key: same one you use for the prod VPS
   - **Tell me**: the new IP address

2. **Add a Cloudflare DNS A record**
   - Hostname: `staging.prospector.leadprospector.ai`
   - Type: A
   - Content: the new VPS IP
   - Proxy status: DNS-only (gray cloud) — Caddy on staging handles TLS itself
   - **Tell me**: that the record is in

3. **Add an SSH alias on your machine**
   ```
   # ~/.ssh/config
   Host vps-staging
     HostName <new IP>
     User root
     IdentityFile ~/.ssh/<your key>
   ```
   - **Tell me**: you can `ssh vps-staging` without a password prompt

## What I'll do after you provision (~1 hour)

Once the VPS + DNS + SSH are ready, I'll handle:

1. **Bootstrap the VPS** — install Python 3.12, Postgres 16, Caddy, systemd
   service config. Same software stack as prod.

2. **Clone + sanitize the prod database** — pull a Postgres dump from prod,
   strip PII (replace real prospect emails with `prospect-{id}@staging.test`,
   nullify phone numbers, clear unsubscribe tokens), restore to staging.
   Staging gets representative data without the risk of accidental real-prospect
   sends.

3. **Configure separate credentials** — staging `.env` gets:
   - Its own Resend API key (Resend has a free tier; sends still go to the
     dedicated `staging-mailbox@aamp.agency` instead of real prospects —
     ENFORCED by a `STAGING_FORCE_RECIPIENT` env var checked in `send_email`)
   - Its own Twilio sub-account (or sandbox mode) so test calls don't dial
     real numbers
   - Its own Claude API key (or share prod's — small cost)
   - Hosts at `staging.prospector.leadprospector.ai`

4. **Set up the deploy script** — `bin/deploy-staging.sh` that pushes the
   current branch to staging. The prod-deploy procedure becomes:
   ```
   bin/deploy-staging.sh        # deploys to staging
   # wait 24h, smoke-check via UI + audit log digest
   bin/deploy-prod.sh           # only after staging verified
   ```

5. **Update the CLAUDE.md root instructions** so future me knows that
   staging is the default target for any deploy unless explicitly prod.

## Safety guard I'll add to send_email

To eliminate the "oops I deployed staging code that talked to a real prospect"
failure mode, `send_email()` will check `STAGING_FORCE_RECIPIENT` on every
call. When set (staging only), the recipient gets force-rewritten to
`staging-mailbox@aamp.agency` regardless of what was passed in. This means:
- Staging code can never email a real prospect, even by mistake
- The audit log still records what the original recipient *would have been*
- You can see exactly what staging *would* have sent to whom

In prod the env var is unset → no rewrite → normal behavior.

## Going-forward workflow (after staging is live)

```
develop:    push to main, my deploy goes to staging
            CI runs tests automatically
            staging app rebuilds in ~30s

stage:      ~24h soak time — I + team use staging URL, check the
            audit-log digest fires correctly on staging-only data

ship:       I run bin/deploy-prod.sh
            Tagged release in git
            Rollback = redeploy previous tag
```

For TRUE emergency fixes (security incident, prod-down): a fast-path
direct-to-prod is fine but the default is staging-first.

## Questions for Steve

- Want me to recommend a specific Hostinger tier (link to their pricing) or
  do you already know what to order?
- Anything else you want staging to be different from prod (auth-gated for
  team-only? IP-allowlist? other?)
