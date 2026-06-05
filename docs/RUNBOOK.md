# BMP Prospector — On-Call Runbook

Last updated: 2026-06-04 after the engagement-engine cutover.

This is the playbook for diagnosing and recovering from production
incidents. Every section answers two questions: **how to detect** and
**how to fix**.

If you're unsure, the safest move is almost always: **stop the workers,
re-enable the legacy engine, restart, investigate.** Reversible in <30s.

---

## Critical aliases

```
ssh vps           → prod (72.62.168.160), backyard-leads service
ssh vps-staging   → staging (2.25.171.108)
```

Working dir on both: `/opt/backyard-leads`
Service: `systemctl {status|restart|stop|start} backyard-leads.service`
Logs (live): `journalctl -u backyard-leads.service -f`
Logs (last hour): `journalctl -u backyard-leads.service --since '1 hour ago'`
Cron logs:
- Dispatcher: `tail -f /var/log/eed-dispatcher.log`
- Decision maker: `tail -f /var/log/eed-decisions.log`
- Signal watcher: `tail -f /var/log/eed-watcher.log`

Env: `/opt/backyard-leads/.env` (mode 600; contains Anthropic, Resend,
Twilio, secret key, DB URL).

---

## 0. Emergency rollback — legacy engine takeover

**When to use:** the new engine is broken in a way that's actively
harming customers (wrong emails sending, dispatcher crash loop,
recipient-lock trigger violations, cross-tenant leakage).

```bash
# 1. Disable the new-engine workers (cron stays but workers gate on env var)
ssh vps 'sed -i "s/^ENGAGEMENT_DISPATCHER_ENABLED=true/ENGAGEMENT_DISPATCHER_ENABLED=false/" /opt/backyard-leads/.env'
ssh vps 'sed -i "s/^ENGAGEMENT_DECISION_MAKER_ENABLED=true/ENGAGEMENT_DECISION_MAKER_ENABLED=false/" /opt/backyard-leads/.env'

# 2. Re-enable the legacy in-process sequence_engine loop
ssh vps 'echo "LEGACY_SEQUENCE_ENGINE_ENABLED=true" >> /opt/backyard-leads/.env'

# 3. Restart
ssh vps 'systemctl restart backyard-leads.service'

# 4. Verify the startup banner
ssh vps 'journalctl -u backyard-leads.service -n 20 --no-pager' | grep "sequence_engine"
# Expect: "legacy sequence_engine ENABLED" instead of "DISABLED".
```

After rollback: existing engagement+actions stay in the DB but stop
firing. Any legacy `generated_emails` rows still in pending state will
start dispatching on the next 60s tick. Inbound webhooks keep flowing.

To return to the new engine: reverse the three flag changes + restart.

**Audit log:** every flag change should be commented in `#prod-ops` with
who/when/why. The .env file isn't versioned, so the channel IS the log.

---

## 1. Workers stopped running

**Detect:**
- Crons log empty for >5 min: `tail -1 /var/log/eed-dispatcher.log`
- `journalctl -u backyard-leads.service` shows no `engagement_engine.dispatcher` lines

**Diagnose:**
```bash
ssh vps 'crontab -l'   # confirm cron lines present
ssh vps 'cd /opt/backyard-leads && /opt/backyard-leads/venv/bin/python -m scripts.run_engagement_dispatcher'
```

The last command runs the dispatcher inline — if it errors, the
stacktrace appears immediately. Common causes:
- ImportError on a renamed module → recent deploy issue, check `git log`
- DB connection failure → check Supabase status page + `psql` directly
- `ENGAGEMENT_DISPATCHER_ENABLED != 'true'` → check `.env`

**Fix:** correct the env / dependency / DB, then `systemctl restart`.

---

## 2. Action stuck — dispatch_heartbeat_at old, status still 'scheduled'

**Detect:**
```sql
SELECT id, status, dispatch_heartbeat_at, scheduled_at,
       NOW() - dispatch_heartbeat_at AS stale_for
FROM actions
WHERE dispatch_heartbeat_at IS NOT NULL
  AND status = 'scheduled'
  AND dispatch_heartbeat_at < NOW() - INTERVAL '5 minutes'
ORDER BY dispatch_heartbeat_at ASC LIMIT 10;
```

If `stale_for` > 60 seconds, the worker died mid-dispatch. The
dispatcher's abandoned-claim recovery picks these up after 60s of no
heartbeat — but if the underlying error keeps recurring, the row will
ping-pong.

**Fix:**
- Inspect the action's `error_message`
- If it's a channel-permanent error (suppression, no contact email):
  manually set status='failed' or status='skipped' with skip_reason
- If it's transient: `UPDATE actions SET dispatch_heartbeat_at = NULL,
  dispatch_worker_id = NULL WHERE id = :id` — the next tick picks it up
  fresh

---

## 3. Resend webhook signature failures

**Detect:**
```
journalctl -u backyard-leads.service --since '10 minutes ago' | grep "bad signature"
```

If the rate is sustained (>5/min), the webhook secret on Resend's side
doesn't match `RESEND_WEBHOOK_SECRET` in `.env`.

**Diagnose:**
```bash
ssh vps 'grep RESEND_WEBHOOK_SECRET /opt/backyard-leads/.env'
```
Compare against the secret shown in Resend dashboard → Webhooks → Edit.

**Fix:**
- If `.env` is correct, regenerate in Resend dashboard, update `.env`,
  restart service.
- If Resend reports retries are accumulating, you may need to manually
  ack/clear the queue from their dashboard.

**Side effect of failed webhooks:** opens/clicks/bounces stop flowing
into signals + Activity. Lead scoring goes stale; suppressions don't
get added; bounces continue sending. **High urgency.**

---

## 4. Twilio inbound flooding

**Detect:**
```
journalctl -u backyard-leads.service --since '10 minutes ago' | grep "/api/twilio/sms" | wc -l
```

If >100/min sustained, something is replay-attacking the inbound
endpoint or a customer's number got into a bot list.

**Fix:**
- Caddy edge (port 80/443) can throttle: see `/etc/caddy/Caddyfile`
  for the rate-limit block already configured for `/api/twilio/*`
- For specific from-numbers, add a STOP rule to the contact's
  do_not_text + write `sms_opt_out` signal manually

---

## 5. Engagement stuck — no action firing

A contact's engagement is `status='active'` but no action ever fires.

**Diagnose:**
```sql
SELECT e.id, e.status, e.last_outreach_at, e.next_action_due_at,
       e.last_reply_at,
       (SELECT COUNT(*) FROM actions a WHERE a.engagement_id = e.id) AS total_actions,
       (SELECT COUNT(*) FROM actions a WHERE a.engagement_id = e.id AND a.status='scheduled') AS scheduled,
       (SELECT COUNT(*) FROM actions a WHERE a.engagement_id = e.id AND a.status='sent')      AS sent,
       (SELECT COUNT(*) FROM actions a WHERE a.engagement_id = e.id AND a.status='skipped')   AS skipped
FROM engagements e WHERE e.contact_id = :contact_id;
```

Common causes:
- `last_reply_at` set → dispatcher's stale-action check blocks further
  sends. Resume the engagement via the BDR's resume button OR manually
  clear: `UPDATE engagements SET last_reply_at = NULL WHERE id = :id;`
- All actions in `status='skipped'` with `skip_reason='no_email'` etc.
  → the contact lacks the channel data. Add email/phone/linkedin then
  re-enroll via `lifecycle.start_engagement`.
- `contact.outreach_owner IN ('paused', 'disputed', 'white_glove')` —
  block by design. Flip to `'engagement_engine'` if intentional.

---

## 6. Recipient-lock trigger violation

**Detect:**
```
journalctl -u backyard-leads.service | grep "enforce_action_recipient"
```

This trigger raises if `actions.recipient_email/phone/linkedin_url`
doesn't match the current `contacts.*` value. It's a hard structural
defense (Texas Remodel Team incident). NEVER bypass.

**Fix:**
- If the contact's email/phone was legitimately updated mid-sequence,
  the action is stale. Mark `status='skipped'` with reason
  `'recipient_changed'` and re-enroll the contact.
- If the action was tampered with (very rare): delete the action, file
  an incident, audit the surrounding transaction.

---

## 7. Cross-tenant data leakage

The auto-filter (`do_orm_execute`) handles ORM SELECTs. Raw `text()`
SQL DOES NOT auto-filter. Every raw lookup in
`app/engagement_engine/lifecycle.py` was hardened post-cutover to join
through `contacts` and enforce `e.tenant_id = c.tenant_id`. But any
NEW raw SQL needs the same treatment.

**Detect:** explicit audit. Grep for `text(` followed by `WHERE` and
verify a tenant predicate is present:
```
grep -rn "text(" app/engagement_engine/ app/routes/ | grep -v __pycache__
```

**Fix:** add `AND tenant_id = :t` to every raw lookup that touches
engagements / actions / signals / contacts. Use the canonical pattern:
```sql
SELECT ... FROM engagements e
JOIN contacts c ON c.id = e.contact_id
WHERE ... AND e.tenant_id = c.tenant_id
```

---

## 8. Dispatcher cross-tenant exposure

`run_dispatcher_tick()` claims due actions ACROSS ALL TENANTS — it's
the cron worker, not a per-tenant action. Endpoints that wrap it must
be either:
- Cron-only (no HTTP route) — currently true for the scripts
- super_admin only (`/api/sequences/run-now`)
- Replaced with a single-action dispatch that's tenant-scoped
  (`/api/integrations/sidebar/send-next-step` — bumps scheduled_at to
  NOW, lets the cron pick it up within 60s)

If anyone proposes adding `run_dispatcher_tick()` to a new endpoint,
push back. Always.

---

## 9. AI costs spiking

**Detect:** sum `actions.ai_generation_cost_usd` and
`signals.ai_scoring_cost_usd` by hour. If burn is 2x baseline, look for:
- A campaign launching with high pre-gen volume (expected — autopilot
  pre-generates 4 emails per enrollment via Claude)
- The decision_maker firing decisions on every signal (each = ~$0.02)
- Infinite loop: signal → decision → action sent → email_open signal →
  decision → ... (this should be blocked by the engagement-level
  `monthly_ai_cost_usd` cap when wired)

**Mitigate:**
- Set `ENGAGEMENT_DECISION_MAKER_ENABLED=false` to stop decisions while
  the engine still dispatches scheduled cadence
- For specific tenants: insert a `tenant_ai_config` row with a small
  budget → the LLMProvider will refuse requests when exceeded

---

## 10. Inbound reply not pausing the engagement

The engagement-engine inbound path (`_route_reply_to_engagement_engine`)
should:
1. Write a `signals.email_reply` row
2. Update `actions.outcome = 'replied'`
3. Update `engagements.last_reply_at = NOW()`
4. The dispatcher's stale-action check then blocks further pending sends

If a contact's BDR says "they replied but kept getting emails":
```sql
SELECT id, engagement_id, last_reply_at FROM engagements WHERE contact_id = :id ORDER BY id DESC LIMIT 1;
SELECT id, status, scheduled_at, outcome FROM actions WHERE engagement_id = :eng ORDER BY scheduled_at;
```
- If `last_reply_at` is NULL → the webhook didn't route to the new
  engine. Check the reply-to token format (`a{action_id}_{hex}`) and
  the `actions.id` referenced.
- If `last_reply_at` is set but actions kept firing → the stale-action
  gate is broken. Check `app/engagement_engine/dispatcher.py` for the
  `last_reply_at` comparison.

**Mitigation while diagnosing:** manually pause:
```python
from app.engagement_engine.lifecycle import pause_engagement
await pause_engagement(db, contact_id, reason="manual pause via runbook")
```

---

## 11. Schema migration failed at startup

App boot does `_apply_migrations()` before serving requests. Failures
crash the service. Check:
```
journalctl -u backyard-leads.service --since '10 minutes ago' | grep "migration"
```

**Common failure:** a column added in code but the prod DB still has
the old schema. Hand-apply the migration via:
```bash
ssh vps 'cd /opt/backyard-leads && /opt/backyard-leads/venv/bin/python -m scripts.migrate_<NAME>'
```
Then restart.

**Note:** drizzle journal drift (TMBT) does not apply here; this is
the BMP repo which uses its own migration registry in
`app/database.py`. Every migration must be both:
- A `scripts/migrate_<name>.py` file
- Registered in `_MIGRATIONS` tuple in `app/database.py`

CI enforces this — the build will reject a new orphan file.

---

## 12. Test before any complex prod change

The two validation scripts are the cheapest pre-prod smoke test:
```bash
ssh vps 'cd /opt/backyard-leads && /opt/backyard-leads/venv/bin/python -m scripts.validate_lifecycle'
ssh vps 'cd /opt/backyard-leads && /opt/backyard-leads/venv/bin/python -m scripts.validate_patches'
```
Both create throwaway test data, exercise the engagement engine
end-to-end, then clean up. Run before deploying anything that touches
`app/engagement_engine/` or sequence routes.

---

## Notable invariants (do not violate)

1. **Recipient-lock trigger** is structural defense — never bypass.
2. **Tenant scoping in raw SQL** must be explicit on every text() lookup.
3. **dispatcher.run_dispatcher_tick** is cron-only or super_admin-only.
4. **last_transition_by** column is VARCHAR(20); truncate at the call site.
5. **skip_reason** is VARCHAR(80); truncate at the call site.
6. **terminal_reason** is VARCHAR(60); truncate at the call site.
7. **idempotency_key** on actions is UNIQUE NOT NULL — use ON CONFLICT DO NOTHING.
8. **Phase transitions** trigger validates by (from, to, allowed_by). The
   `system` actor has a NARROWER allowlist than `bdr`; prefer `bdr` for
   lifecycle helpers unless system is truly correct.
9. **Never re-enable password SSH** on the VPS.
10. **Push to GitHub every iteration** of a working session (Steve's
    standing instruction).

---

## Contact

- Steve Edwards (CEO, primary on-call): `steve@aamp.agency`
- Sentry org: `take-my-boat-test-llc`, project: `ai-prospector`
- DB: Supabase project (URL in `.env`)
- Resend dashboard, Twilio console: linked in 1Password vault "BMP Ops"
