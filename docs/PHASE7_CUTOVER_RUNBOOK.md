# Phase 7 Cutover Runbook

Operational guide for migrating BMP from the legacy sequence engine to the
new engagement engine. **Read this fully before running any commands.**

## Prerequisites checklist

- [ ] Phase 0–6 code is on prod (deployed from `main`)
- [ ] Prod has been restarted at least once since the deploy (so
      `migrate_engagement_engine_v1.py` ran via the migration chain)
- [ ] `verify_engagement_engine_v1.py` shows 65/65 on prod
- [ ] CI is green
- [ ] Steve has briefed the BDR team using the template from the design doc
- [ ] You know which contact you want as the canary

## The 7 operations (in order)

All commands run from `/opt/backyard-leads` on the prod VPS with the venv
activated.

### 1. Validate prod (read-only)

```bash
python -m scripts.cutover_phase7 validate-prod
```

Expect: `65 passed, 0 failed`. If anything red, stop — fix before continuing.

### 2. Backfill (one-time)

Imports `seq_templates` → playbooks, auto-creates `tenant_ai_config`
defaults, creates an `engagement` row for every active `seq_enrollment`.

Always dry-run first:

```bash
python -m scripts.cutover_phase7 backfill --dry-run
```

Inspect the log. When happy:

```bash
python -m scripts.cutover_phase7 backfill
```

**What this DOES NOT do**: flip any contact's `outreach_owner`. After
backfill, every contact is still owned by the legacy engine. The new
engine has data ready but is idle.

### 3. Enable the workers (cron + env)

```bash
python -m scripts.cutover_phase7 enable-workers
```

This **prints** the env-var and cron lines to install. Set the env vars
in `/opt/backyard-leads/.env`:

```
ENGAGEMENT_DISPATCHER_ENABLED=true
ENGAGEMENT_WATCHER_ENABLED=true
ENGAGEMENT_DECISION_MAKER_ENABLED=true
```

Add the cron lines via `crontab -e`:

```cron
* * * * * cd /opt/backyard-leads && /opt/backyard-leads/venv/bin/python -m scripts.run_engagement_dispatcher >> /var/log/eed-dispatcher.log 2>&1
* * * * * cd /opt/backyard-leads && /opt/backyard-leads/venv/bin/python -m scripts.run_engagement_decision_maker >> /var/log/eed-decisions.log 2>&1
*/5 * * * * cd /opt/backyard-leads && /opt/backyard-leads/venv/bin/python -m scripts.run_engagement_signal_watcher >> /var/log/eed-watcher.log 2>&1
```

**At this point** the new engine cron is running but every contact's
`outreach_owner='legacy'` means the dispatcher just fetches 0 actions
each tick. No actual sends — the system is "armed but inactive."

### 4. Canary flip — ONE contact

Pick a low-stakes prospect from BMP's pipeline. Get their contact_id from
the CRM, then:

```bash
# Dry run first
python -m scripts.cutover_phase7 flip-batch --contact-ids 12345 --dry-run

# Then actually flip
python -m scripts.cutover_phase7 flip-batch --contact-ids 12345 \
    --notes "Day 1 canary — Sebastian's low-stakes test prospect"
```

Wait **at least 1 hour**. The signal watcher will start polling that
contact's company. The decision_maker may score signals. The dispatcher
will (eventually) execute scheduled actions.

Watch the logs:
```bash
tail -f /var/log/eed-*.log
```

### 5. Check metrics

```bash
python -m scripts.cutover_phase7 metrics --hours 24
```

Compares old engine vs new engine for the last 24 hours. The script
prints a recommendation:
- `✓ Metrics look healthy. Safe to expand batch.`
- `⚠️  WARNING: new engine reply rate is < 50% of old. Consider rollback.`

Sample size of 1 is too small to be confident — use this script after
each batch expansion, not just the first canary.

### 6. Batched expansion

The recommended ramp (adjust based on observed metrics):

| Day | Batch size | Cumulative |
|---|---|---|
| 1 | 1 (canary) | 1 |
| 2 | 10 | 11 |
| 3 | 50 | 61 |
| 4 | 200 | 261 |
| 5 | 500 | 761 |
| 6 | 1000 | 1761 |
| 7 | remaining | ~2000 |

Run between batches:
```bash
python -m scripts.cutover_phase7 flip-batch --count 50 \
    --notes "Day 3 expansion"
```

After each batch, run `metrics` and let it sit for at least a few hours
before expanding again.

### 7. Rollback (emergency only)

If metrics tank, contacts complain, or anything looks weird:

```bash
# Rollback one specific contact
python -m scripts.cutover_phase7 rollback --contact-ids 12345

# Rollback the most-recently-flipped N
python -m scripts.cutover_phase7 rollback --count 50

# In dire emergencies: rollback everyone (defensive)
python -m scripts.cutover_phase7 rollback --count 99999
```

Rollback:
- Flips `outreach_owner` back to `'legacy'`
- Marks any in-flight new-engine `actions` as `'blocked'`
- Resumes the legacy `seq_enrollments` we paused at flip time

After rollback, the legacy engine resumes processing those contacts
within ~5 minutes (its next scheduled tick).

## What to watch for

### Healthy signs
- Decision_maker tick logs showing `signals_scored: N, total_cost_usd: ...`
- Dispatcher tick logs showing `sent: N`
- BDR sees their pipeline view unchanged
- Reply rate ≥ legacy baseline
- Meeting-set rate ≥ legacy baseline
- No surge in `cutover_audit` rollback entries

### Warning signs
- Decision_maker `provider_failures > 0` → check ANTHROPIC_API_KEY
- Dispatcher `blocked: high count, reason='recipient_drift_*'` → contact
  email changes during cutover; investigate
- Reply rate drops > 30% from legacy → rollback the latest batch
- Customer complaints about email content → review AI-generated outputs
  in `ai_decisions` table

### Debugging queries

```sql
-- Contacts currently on new engine
SELECT COUNT(*) FROM contacts WHERE outreach_owner = 'engagement_engine';

-- Recent cutover operations
SELECT * FROM cutover_audit ORDER BY performed_at DESC LIMIT 10;

-- Engine activity in the last hour
SELECT
    SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent,
    SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) AS blocked,
    SUM(CASE WHEN status='scheduled' THEN 1 ELSE 0 END) AS pending
FROM actions
WHERE created_at > NOW() - INTERVAL '1 hour';

-- AI decision cost last 24h
SELECT
    decision_type, model_used,
    COUNT(*) as calls,
    SUM(cost_usd) as cost
FROM ai_decisions
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY decision_type, model_used
ORDER BY cost DESC;

-- Top recent signals
SELECT
    s.id, st.code, s.relevance_score, s.ai_summary, s.detected_at
FROM signals s
JOIN signal_types st ON st.id = s.signal_type_id
WHERE s.detected_at > NOW() - INTERVAL '24 hours'
ORDER BY s.relevance_score DESC NULLS LAST, s.detected_at DESC
LIMIT 20;
```

## Retirement (Day 30+)

Once 100% of contacts have been on the new engine for 30+ consecutive days
with stable metrics:

1. Delete or comment-out the legacy `process_pending_steps` cron
2. Mark `seq_templates.is_active = FALSE` for all imported templates
3. Plan deletion of the legacy engine code paths in a follow-up PR
   (keep the SCHEMA — historical generated_emails rows are still useful
   for audit / reply attribution)

**Do NOT delete the schema for at least 90 days** in case we need to
forensically reconstruct what the legacy engine did during the cutover
window.

## The team message (verbatim, post-cutover-day-1)

> "Heads up — we just flipped one contact (Tim @ XYZ Pools) to the new
> engagement engine. Over the next week we'll expand to 10, then 50, then
> 200, etc. You won't notice anything in your CRM — same leads, same
> pipeline. Two new surfaces:
>
> 1. **Approval queue** at `/api/engagement/inbound-unattributed` (UI TBD)
>    — when AI drafts a high-stakes message, it lands there for your
>    sign-off. Check daily.
>
> 2. **Signal feed** — real-time alerts for stuff happening at prospect
>    companies. Glance at it each morning; that's where the AI catches
>    things we'd miss.
>
> If anything looks weird — Slack me immediately. We can flip any
> contact back to the old engine in seconds."
