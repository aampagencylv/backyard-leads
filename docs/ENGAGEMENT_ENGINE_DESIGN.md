# LeadProspector Engagement Engine — Design Document

**Status**: Phase 0 draft. Awaiting Steve sign-off before Phase 1 begins.
**Author**: Claude (CTO agent) + Steve Edwards
**Last updated**: 2026-06-03

---

## Executive Summary

The current Prospector sequence engine is a linear, calendar-driven system. A
contact gets enrolled in a 30-day sequence, steps fire on day 0, day 3, day 7,
etc., and at day 30 the sequence completes. This model breaks down for what
LeadProspector actually needs to do: **pursue a lead until they become a
customer or tell us no — possibly for 12+ months — using AI to react to
real-world signals about the prospect.**

This document describes the rebuild: a **continuous lead nurture engine** where
the unit of work is an *engagement* (a long-running pursuit of a single
contact), driven by *signals* observed about the prospect (LinkedIn updates,
GMB changes, website edits, hiring posts, our own engagement data), with *AI
decisions* that turn signals into *actions* (email, SMS, LinkedIn message, BDR
task) across any channel.

The current `seq_*` schema (4 tables built in the prior phase) is preserved as
one **playbook type** within the new system — a "linear sequence playbook" for
the cold-outreach phase of an engagement. It is not thrown away or refactored;
it is wrapped.

**Build approach**: in-place rebuild inside the existing Prospector repo,
treated mentally as a greenfield engagement-engine module. Auth, tenant
isolation, CRM UI shell, integrations (Resend, Twilio, Google Places, Sentry,
audit log) are reused. The engine is a new module: `app/engagement_engine/`.

**Estimated timeline**: 6–8 weeks of focused work across 9 phases (0–8), each
gated by an explicit GO/NO-GO sign-off.

**Estimated cost (BMP @ ~2000 active engagements)**:
- Opus-everywhere: $4–10K/mo
- Balanced (Opus for decisions, Haiku for scoring): $1.5–3K/mo
- Cost-optimized (DeepSeek/Llama for most work via OpenRouter): $500–1K/mo

The tenant chooses the dial via BYO AI configuration (Rule #11).

---

## The 11 Immutable Rules

These are non-negotiable. Any future feature that would violate one of these
must propose revising the rule openly, not sneak around it.

1. **Additive-only schema.** No DROP, no breaking ALTER on existing columns.
   Old code keeps reading old tables until cutover. New columns/tables only.

2. **Idempotency keys on every dispatch.** Every signal observation, action,
   and AI decision has a UNIQUE key at the DB layer. Code bugs cannot
   double-anything.

3. **Tenant isolation at the DB layer.** Every new table has `tenant_id` + the
   RLS policy. Application code is a second line of defense.

4. **Enums enforced as DB CHECK constraints.** Postgres-level, not just Python
   enums. The Texas Remodel Team incident proved this matters.

5. **Event-sourced for the important stuff.** Signals, actions, AI decisions
   are immutable logs. Current state (`engagement.current_phase`) is derived
   and cached; the log is the source of truth and can rebuild state.

6. **Channels are pluggable.** All outbound channels conform to the
   `ActionDispatcher` interface. Adding a channel means implementing the
   interface, not editing the engine.

7. **Signal sources are pluggable.** All inbound signal sources conform to the
   `SignalSource` interface. Adding a source is config + an adapter.

8. **AI decisions are fully auditable.** Every LLM call that affects a
   prospect captures input context, output choice, reasoning, model used,
   provider, cost. We can always answer "why did the system do X?"

9. **Hard kill-switch per engagement and per company.** Setting
   `company.do_not_contact = TRUE` or `engagement.status = 'terminal'` halts
   all outreach at action-dispatch time. No race conditions, no "but the
   worker had already scheduled it."

10. **Cost budgeting at the engagement level.** Each engagement has a monthly
    LLM cost cap (configurable per tenant). Exceeding the cap pauses the
    engagement and notifies the BDR. No surprise bills.

11. **BYO AI per tenant.** No hardcoded LLM provider. The engine treats LLM
    access as a configurable resource per tenant — same as Resend or Twilio.
    `LLMProvider` interface with adapters for Anthropic, OpenAI, OpenRouter,
    Google Gemini at launch; Ollama/vLLM/NIM/Bedrock added later. Prompts
    must be portable across models (no Claude-specific features in the core
    decision loop). Tenant chooses per-task model assignment. Default behavior
    when no tenant config: use AAMP's Anthropic key + bill tenant marked-up
    rate (until BYO UI ships).

---

## Conceptual Model

```
                    ┌───────────────────────────────────────┐
                    │       PROSPECT ENGAGEMENT              │
                    │  (one per contact; runs months/years)  │
                    │                                        │
                    │  Phase: cold_outreach → meeting_set    │
                    │       → nurture → qualified → customer │
                    │                                        │
                    │  Current playbook + position           │
                    │  Engagement score (AI-computed)        │
                    │  Next signal check, next action due    │
                    └───────────────────────────────────────┘
                              ↓                  ↑
                              ↓                  ↑
            ┌──────────────────────┐  ┌──────────────────────┐
            │   SIGNALS (inbound)  │  │  ACTIONS (outbound)  │
            │                      │  │                      │
            │  • LinkedIn updates  │  │  • Email             │
            │  • GMB changes       │  │  • SMS               │
            │  • Website edits     │  │  • LinkedIn message  │
            │  • Hiring posts      │  │  • BDR call task     │
            │  • News / press      │  │  • Manual outreach   │
            │  • Email opens       │  │                      │
            │  • Email replies     │  │  Captured at-time:   │
            │  • Call outcomes     │  │   subject, body,     │
            │                      │  │   recipient, etc.    │
            │  Each has:           │  │                      │
            │   relevance score    │  │  Idempotency key     │
            │   AI summary         │  │   prevents double    │
            └──────────────────────┘  └──────────────────────┘
                       ↓                       ↑
                       ↓                       ↑
                    ┌────────────────────────────┐
                    │       AI DECISION           │
                    │                             │
                    │  Input: engagement history  │
                    │         recent signals      │
                    │         BDR notes           │
                    │         current phase       │
                    │                             │
                    │  Output: what action +      │
                    │          when + why         │
                    │                             │
                    │  Audited: model, provider,  │
                    │           cost, reasoning   │
                    └────────────────────────────┘
```

**Key conceptual moves vs the old sequence engine:**

- The unit of work changes from "step in a sequence" to "engagement with a
  prospect."
- Time horizon changes from "30 days" to "until terminal state."
- Decision-making shifts from "calendar fires next step" to "AI reads signals
  + decides what to do."
- Content changes from "pre-written template" to "AI-generated at action
  time" (with template seeding for cold outreach).
- A "sequence" becomes one *playbook* — a reusable strategy for a phase of
  engagement.

---

## Full Schema (8 New Tables)

All tables are additive. They live alongside the existing `seq_*`,
`generated_emails`, `contacts`, `companies`, `tenants`, `users` tables. No
existing table is modified.

### 1. `engagements` — THE central table

```sql
CREATE TABLE engagements (
    id                     SERIAL PRIMARY KEY,
    tenant_id              INTEGER NOT NULL,
    contact_id             INTEGER NOT NULL REFERENCES contacts(id),
    company_id             INTEGER NOT NULL REFERENCES companies(id),
    current_phase          VARCHAR(40) NOT NULL DEFAULT 'cold_outreach'
                             CHECK (current_phase IN (
                               'cold_outreach','meeting_set','post_meeting_nurture',
                               'qualified','customer','declined','lost','dormant'
                             )),
    current_playbook_id    INTEGER REFERENCES playbooks(id),
    current_action_index   INTEGER NOT NULL DEFAULT 0,
    status                 VARCHAR(20) NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active','paused','hibernating','terminal')),
    terminal_reason        VARCHAR(60),  -- when status='terminal'
    next_action_due_at     TIMESTAMPTZ,
    next_signal_check_at   TIMESTAMPTZ,
    last_outreach_at       TIMESTAMPTZ,
    last_signal_at         TIMESTAMPTZ,
    last_reply_at          TIMESTAMPTZ,
    assigned_bdr_id        INTEGER REFERENCES users(id),
    engagement_score       INTEGER NOT NULL DEFAULT 50
                             CHECK (engagement_score BETWEEN 0 AND 100),
    tier                   VARCHAR(10) NOT NULL DEFAULT 'warm'
                             CHECK (tier IN ('hot','warm','cold','dormant')),
    ai_engagement_summary  TEXT,  -- LLM-maintained 1-paragraph "where we are"
    notes                  TEXT,  -- BDR-editable freeform
    monthly_ai_cost_usd    NUMERIC(10,4) NOT NULL DEFAULT 0,
    monthly_ai_cost_reset_at TIMESTAMPTZ,
    started_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    terminal_at            TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX ux_engagements_one_active_per_contact
  ON engagements (contact_id) WHERE status != 'terminal';
CREATE INDEX ix_engagements_due
  ON engagements (status, next_action_due_at) WHERE status = 'active';
CREATE INDEX ix_engagements_signal_check
  ON engagements (status, next_signal_check_at) WHERE status = 'active';
CREATE INDEX ix_engagements_tenant_phase
  ON engagements (tenant_id, current_phase, status);
```

**Why these choices:**
- `current_phase` is enum-enforced with 8 values covering full lifecycle
- `status='terminal'` is the kill switch (Rule #9)
- `engagement_score` 0–100 lets BDR + AI surface hot leads
- `tier` drives polling frequency in the signal watcher
- `monthly_ai_cost_usd` enforces Rule #10 (cost budgeting)
- `ai_engagement_summary` is the 1-paragraph context that's cheap to feed into
  every decision call (vs re-fetching full history)
- UNIQUE constraint prevents two active engagements per contact

### 2. `playbooks` — reusable strategies

```sql
CREATE TABLE playbooks (
    id                     SERIAL PRIMARY KEY,
    tenant_id              INTEGER,  -- NULL = system-wide template
    name                   VARCHAR(200) NOT NULL,
    description            TEXT,
    phase                  VARCHAR(40) NOT NULL
                             CHECK (phase IN (
                               'cold_outreach','meeting_set','post_meeting_nurture',
                               'qualified','customer','declined','lost','dormant',
                               'cross_phase'
                             )),
    mode                   VARCHAR(20) NOT NULL
                             CHECK (mode IN (
                               'linear_sequence','signal_driven','hybrid','trigger_response'
                             )),
    duration_max_days      INTEGER,  -- NULL = indefinite
    ai_strategy_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
    legacy_seq_template_id INTEGER REFERENCES seq_templates(id),  -- for migrated playbooks
    is_active              BOOLEAN NOT NULL DEFAULT TRUE,
    version                INTEGER NOT NULL DEFAULT 1,
    parent_playbook_id     INTEGER REFERENCES playbooks(id),  -- for version chain
    created_by_user_id     INTEGER REFERENCES users(id),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_playbooks_tenant_phase ON playbooks (tenant_id, phase, is_active);
CREATE INDEX ix_playbooks_legacy ON playbooks (legacy_seq_template_id) WHERE legacy_seq_template_id IS NOT NULL;
```

**Why these choices:**
- `mode` distinguishes linear vs signal-driven vs hybrid (handles all use cases)
- `legacy_seq_template_id` lets migrated `seq_templates` become playbooks
  without copy
- `version` + `parent_playbook_id` enable Tier 1 feature: per-enrollment
  version pinning. Editing a playbook creates a new version; existing
  engagements keep using the version they were enrolled into.

### 3. `playbook_actions` — what the playbook does

```sql
CREATE TABLE playbook_actions (
    id                       SERIAL PRIMARY KEY,
    playbook_id              INTEGER NOT NULL REFERENCES playbooks(id) ON DELETE CASCADE,
    tenant_id                INTEGER,
    action_order             INTEGER NOT NULL,
    channel                  VARCHAR(20) NOT NULL
                               CHECK (channel IN (
                                 'email','sms','linkedin','call_task','wait','manual'
                               )),
    trigger                  VARCHAR(40) NOT NULL DEFAULT 'scheduled'
                               CHECK (trigger IN (
                                 'scheduled','on_signal','on_no_engagement_for_n_days',
                                 'on_phase_transition','on_reply_intent'
                               )),
    trigger_config_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
    ai_personalization_mode  VARCHAR(20) NOT NULL DEFAULT 'augmented'
                               CHECK (ai_personalization_mode IN (
                                 'none','augmented','generated_from_context'
                               )),
    subject_template         TEXT,
    body_template            TEXT,
    task_template            TEXT,
    day_offset               INTEGER NOT NULL DEFAULT 0,
    skip_conditions_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
    legacy_seq_step_id       INTEGER REFERENCES seq_template_steps(id),
    is_active                BOOLEAN NOT NULL DEFAULT TRUE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX ux_playbook_actions_order
  ON playbook_actions (playbook_id, action_order) WHERE is_active = TRUE;
CREATE INDEX ix_playbook_actions_pb ON playbook_actions (playbook_id, action_order);
```

### 4. `signals` — inbound observation log (event-sourced)

```sql
CREATE TABLE signals (
    id                     SERIAL PRIMARY KEY,
    tenant_id              INTEGER NOT NULL,
    engagement_id          INTEGER NOT NULL REFERENCES engagements(id),
    signal_type            VARCHAR(40) NOT NULL
                             CHECK (signal_type IN (
                               'linkedin_profile_change','linkedin_post',
                               'linkedin_company_update','gmb_review','gmb_post',
                               'gmb_listing_change','website_change','website_new_page',
                               'hiring_signal','press_mention','news_mention',
                               'email_open','email_click','email_reply',
                               'sms_reply','call_outcome','manual_note',
                               'meeting_booked','meeting_completed','meeting_no_show'
                             )),
    source_url             TEXT,
    source_endpoint        VARCHAR(80),
    raw_data_json          JSONB NOT NULL,
    observed_at            TIMESTAMPTZ NOT NULL,
    detected_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    relevance_score        INTEGER CHECK (relevance_score BETWEEN 0 AND 100),
    ai_summary             TEXT,
    ai_scored_by_model     VARCHAR(60),
    ai_scoring_cost_usd    NUMERIC(8,5),
    triggered_action_id    INTEGER REFERENCES actions(id),
    idempotency_key        VARCHAR(200) NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX ux_signals_idempotency ON signals (idempotency_key);
CREATE INDEX ix_signals_engagement ON signals (engagement_id, detected_at DESC);
CREATE INDEX ix_signals_unscored ON signals (relevance_score) WHERE relevance_score IS NULL;
CREATE INDEX ix_signals_high_relevance
  ON signals (engagement_id, relevance_score DESC, detected_at DESC)
  WHERE relevance_score >= 70;
```

**Why these choices:**
- 20 signal types covering external + internal observations
- `idempotency_key` = `f"{source_endpoint}-{engagement_id}-{snapshot_hash}"`
  prevents duplicate ingestion of the same observation
- `raw_data_json` preserves the original payload for re-scoring with a better
  model later
- `triggered_action_id` ties signal → action when the signal caused an action

### 5. `actions` — outbound dispatch log (event-sourced)

```sql
CREATE TABLE actions (
    id                       SERIAL PRIMARY KEY,
    tenant_id                INTEGER NOT NULL,
    engagement_id            INTEGER NOT NULL REFERENCES engagements(id),
    playbook_action_id       INTEGER REFERENCES playbook_actions(id),  -- NULL = ad-hoc
    triggered_by_signal_id   INTEGER REFERENCES signals(id),  -- NULL = scheduled
    triggered_by_decision_id INTEGER REFERENCES ai_decisions(id),
    channel                  VARCHAR(20) NOT NULL
                               CHECK (channel IN (
                                 'email','sms','linkedin','call_task','manual'
                               )),
    status                   VARCHAR(20) NOT NULL DEFAULT 'scheduled'
                               CHECK (status IN (
                                 'scheduled','sent','failed','skipped',
                                 'completed','blocked','awaiting_approval'
                               )),
    requires_human_review    BOOLEAN NOT NULL DEFAULT FALSE,
    approved_by_user_id      INTEGER REFERENCES users(id),
    approved_at              TIMESTAMPTZ,
    scheduled_at             TIMESTAMPTZ NOT NULL,
    executed_at              TIMESTAMPTZ,
    subject                  VARCHAR(500),
    body                     TEXT,
    task_description         TEXT,
    recipient_email          VARCHAR(320),
    recipient_phone          VARCHAR(40),
    recipient_linkedin_url   VARCHAR(500),
    idempotency_key          VARCHAR(200) NOT NULL,
    external_id              VARCHAR(120),  -- resend_message_id, twilio_call_sid, etc.
    error_message            TEXT,
    skip_reason              VARCHAR(80),
    outcome                  VARCHAR(40),  -- opened, clicked, replied, etc.
    outcome_observed_at      TIMESTAMPTZ,
    ai_strategy_used         VARCHAR(40),  -- which decision mode created this
    ai_generation_cost_usd   NUMERIC(8,5),
    send_cost_usd            NUMERIC(8,5),
    sent_by_user_id          INTEGER REFERENCES users(id),  -- for manual sends
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX ux_actions_idempotency ON actions (idempotency_key);
CREATE INDEX ix_actions_scheduled
  ON actions (status, scheduled_at)
  WHERE status IN ('scheduled','awaiting_approval');
CREATE INDEX ix_actions_engagement ON actions (engagement_id, scheduled_at DESC);
CREATE INDEX ix_actions_external_id ON actions (external_id) WHERE external_id IS NOT NULL;
```

**Why these choices:**
- `requires_human_review` enables Tier 2 send-approval gate
- `idempotency_key` UNIQUE is the structural double-send prevention
- Captures all 3 channel-specific recipient fields (email, phone, linkedin)
  in one row — channels NULL their irrelevant fields
- `outcome` is freeform varchar with curated values so we don't need a
  migration every time we want to track a new outcome type

### 6. `ai_decisions` — full audit trail of AI choices

```sql
CREATE TABLE ai_decisions (
    id                       SERIAL PRIMARY KEY,
    tenant_id                INTEGER NOT NULL,
    engagement_id            INTEGER NOT NULL REFERENCES engagements(id),
    signal_id                INTEGER REFERENCES signals(id),  -- NULL if proactive
    decision_type            VARCHAR(40) NOT NULL
                               CHECK (decision_type IN (
                                 'score_signal_relevance','what_to_send',
                                 'when_to_send','classify_reply','draft_reply',
                                 'recommend_playbook_switch','recommend_phase_transition',
                                 'recommend_tier_change','recommend_pause',
                                 'generate_engagement_summary','generate_content',
                                 'select_next_step','detect_fatigue'
                               )),
    input_context_json       JSONB NOT NULL,
    output_choice_json       JSONB NOT NULL,
    reasoning                TEXT,
    provider                 VARCHAR(40) NOT NULL,
    model_used               VARCHAR(80) NOT NULL,
    tokens_in                INTEGER,
    tokens_out               INTEGER,
    cost_usd                 NUMERIC(8,5),
    latency_ms               INTEGER,
    human_override_action_id INTEGER REFERENCES actions(id),
    idempotency_key          VARCHAR(200) NOT NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX ux_ai_decisions_idempotency ON ai_decisions (idempotency_key);
CREATE INDEX ix_ai_decisions_engagement ON ai_decisions (engagement_id, created_at DESC);
CREATE INDEX ix_ai_decisions_cost_today
  ON ai_decisions (tenant_id, created_at) WHERE created_at > NOW() - INTERVAL '24 hours';
```

**Why these choices:**
- 13 decision types cover the full AI surface
- Captures provider + model + cost per call (Rule #11, BYO AI accounting)
- `human_override_action_id` records when BDR overrode the AI choice
- Idempotency key prevents duplicate decisions if the worker retries

### 7. `observations` — polling job scheduler

```sql
CREATE TABLE observations (
    id                     SERIAL PRIMARY KEY,
    tenant_id              INTEGER NOT NULL,
    engagement_id          INTEGER NOT NULL REFERENCES engagements(id),
    source_type            VARCHAR(40) NOT NULL
                             CHECK (source_type IN (
                               'linkedin_profile','linkedin_company','linkedin_posts',
                               'gmb_listing','website_homepage','website_careers',
                               'hiring_indeed','hiring_glassdoor','news_mentions',
                               'yelp_listing','facebook_page','instagram_profile'
                             )),
    source_url             TEXT NOT NULL,
    last_polled_at         TIMESTAMPTZ,
    next_poll_at           TIMESTAMPTZ NOT NULL,
    poll_interval_days     INTEGER NOT NULL DEFAULT 7,
    last_snapshot_hash     VARCHAR(64),
    last_snapshot_at       TIMESTAMPTZ,
    is_active              BOOLEAN NOT NULL DEFAULT TRUE,
    consecutive_failures   INTEGER NOT NULL DEFAULT 0,
    last_error             TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX ux_observations_engagement_source
  ON observations (engagement_id, source_type) WHERE is_active = TRUE;
CREATE INDEX ix_observations_due
  ON observations (next_poll_at, is_active) WHERE is_active = TRUE;
```

### 8. `tenant_ai_config` — BYO AI per Rule #11

```sql
CREATE TABLE tenant_ai_config (
    tenant_id                      INTEGER PRIMARY KEY,
    provider                       VARCHAR(40) NOT NULL DEFAULT 'aamp_default'
                                     CHECK (provider IN (
                                       'aamp_default','anthropic','openai','openrouter',
                                       'google_gemini','ollama','vllm','nim',
                                       'bedrock','azure_openai','together','fireworks','groq'
                                     )),
    api_key_encrypted              TEXT,  -- Fernet, NULL when provider='aamp_default'
    base_url                       VARCHAR(200),  -- NULL = provider default
    model_signal_scoring           VARCHAR(80) NOT NULL DEFAULT 'claude-haiku-4-5',
    model_reply_classification     VARCHAR(80) NOT NULL DEFAULT 'claude-haiku-4-5',
    model_content_generation       VARCHAR(80) NOT NULL DEFAULT 'claude-sonnet-4-6',
    model_decision_making          VARCHAR(80) NOT NULL DEFAULT 'claude-opus-4-7',
    model_engagement_summary       VARCHAR(80) NOT NULL DEFAULT 'claude-sonnet-4-6',
    monthly_budget_usd             NUMERIC(10,2),
    per_engagement_budget_usd      NUMERIC(8,4) NOT NULL DEFAULT 5.00,
    fallback_provider              VARCHAR(40),
    current_month_spent_usd        NUMERIC(10,4) NOT NULL DEFAULT 0,
    current_month_reset_at         TIMESTAMPTZ,
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

## The 3 Worker Processes

The engine is three cooperating workers, each runnable independently. They
communicate through the DB (not in-process queues) so any can be paused,
crashed, or scaled separately without breaking the others.

### Worker A — Signal Watcher

**Run cadence**: every 5 minutes
**Reads**: `observations` where `next_poll_at <= NOW() AND is_active`
**Writes**: `signals` (when something changed); updates `observations` with
new snapshot hash and next poll time

```python
async def tick():
    due = await fetch_due_observations(limit=100)
    for obs in due:
        source = signal_source_registry[obs.source_type]
        try:
            current_snapshot = await source.fetch(obs.source_url)
            current_hash = hash(current_snapshot)
            if current_hash != obs.last_snapshot_hash:
                # something changed
                signals_extracted = source.extract_signals(
                    prev=obs.last_snapshot, current=current_snapshot
                )
                for sig in signals_extracted:
                    await persist_signal(
                        engagement_id=obs.engagement_id,
                        signal_type=sig.type,
                        raw_data=sig.data,
                        idempotency_key=f"{obs.source_type}-{obs.engagement_id}-{current_hash}-{sig.idx}",
                    )
            await update_observation(obs, hash=current_hash, next_poll=compute_next(obs))
        except SourceError as e:
            await mark_failure(obs, e)
```

**Tier-based polling cadence** (set per-engagement based on `tier`):
- Hot: daily
- Warm: weekly
- Cold: bi-weekly
- Dormant: monthly

### Worker B — Decision Maker

**Run cadence**: every 1 minute
**Reads**:
  - `signals` where `relevance_score IS NULL` (score them)
  - `signals` where `relevance_score >= 70` and not yet acted on (decide)
  - `engagements` where `next_action_due_at <= NOW()` (proactive checks)
**Writes**: `ai_decisions`, `actions`

```python
async def tick():
    # 1. Score newly-arrived signals (cheap model)
    unscored = await fetch_unscored_signals(limit=50)
    for sig in unscored:
        score, summary = await llm.score_signal(sig)
        await persist_decision(decision_type='score_signal_relevance', ...)
        await update_signal(sig, score=score, summary=summary)

    # 2. React to high-relevance signals (expensive model)
    high_rel = await fetch_high_relevance_unacted_signals(limit=20)
    for sig in high_rel:
        eng = await fetch_engagement(sig.engagement_id)
        if not check_cost_budget(eng):
            await pause_engagement(eng, reason='cost_budget_exceeded')
            continue
        decision = await llm.decide_action(eng, sig)
        await persist_decision(...)
        if decision.should_act:
            await persist_action(
                engagement_id=eng.id,
                channel=decision.channel,
                scheduled_at=decision.timing,
                subject=decision.subject,
                body=decision.body,
                requires_human_review=decision.requires_review,
                idempotency_key=f"sig-{sig.id}-decision-{decision.id}",
            )

    # 3. Proactive engagement checks (scheduled visits)
    due_engagements = await fetch_due_engagement_checks(limit=20)
    for eng in due_engagements:
        decision = await llm.proactive_check(eng)
        # similar persist...
```

### Worker C — Action Dispatcher

**Run cadence**: every 30 seconds
**Reads**: `actions` where `status='scheduled' AND scheduled_at <= NOW() AND NOT requires_human_review`
**Writes**: updates `actions` with dispatch outcome

```python
async def tick():
    due = await fetch_due_actions(limit=20)
    for act in due:
        # Guard 1: company-level kill switch
        company = await fetch_company(act.engagement_id)
        if company.do_not_contact:
            await mark_blocked(act, reason='company_do_not_contact')
            continue

        # Guard 2: engagement-level kill switch
        eng = await fetch_engagement(act.engagement_id)
        if eng.status == 'terminal':
            await mark_blocked(act, reason='engagement_terminal')
            continue

        # Guard 3: channel-specific guards (e.g., email anomaly score)
        channel = channel_registry[act.channel]
        guard_result = await channel.pre_dispatch_guards(act)
        if guard_result.blocked:
            await mark_blocked(act, reason=guard_result.reason)
            continue

        # Dispatch
        try:
            result = await channel.send(act)
            await mark_sent(act, external_id=result.external_id)
            await update_engagement_last_outreach(eng)
            await persist_audit_log(...)
        except TransientError:
            # retry later, don't mark failed
            await reschedule(act, delay_seconds=300)
        except PermanentError as e:
            await mark_failed(act, error=str(e))
```

---

## Channel Interface

```python
class ActionDispatcher(Protocol):
    channel: str  # 'email', 'sms', 'linkedin', 'call_task', 'manual'

    async def pre_dispatch_guards(self, action: Action) -> GuardResult:
        """Channel-specific safety checks before send.

        Email: anomaly score, recipient validation, do-not-contact check,
        STAGING_FORCE_RECIPIENT rewrite, empty subject guard, placeholder
        regex check.

        SMS: rate limit, opt-out check, valid phone format.

        LinkedIn: connection-status check, weekly cap.

        Call task: just validates the task can be assigned to a BDR.

        Manual: always passes (BDR handles).
        """

    async def send(self, action: Action) -> SendResult:
        """Actually dispatch via the channel's underlying transport.

        Returns: SendResult(external_id, success, error_message)
        """

    async def fetch_outcome(self, action: Action) -> OutcomeUpdate | None:
        """Poll for outcome updates (opens, clicks, replies).

        Called by a separate outcome-poller worker (or webhook-driven for
        providers that support it like Resend).
        """
```

**Adapters built day 1**: `EmailChannel`, `SMSChannel`, `BDRTaskChannel`,
`ManualChannel`.
**Adapter Phase 8**: `LinkedInChannel`.

---

## Signal Source Interface

```python
class SignalSource(Protocol):
    source_type: str
    poll_interval_default_days: int  # Hot tier overrides via observations table

    async def fetch(self, url: str) -> Snapshot:
        """Fetch raw current state of the source."""

    def extract_signals(
        self, prev: Snapshot | None, current: Snapshot
    ) -> list[ExtractedSignal]:
        """Diff prev vs current to produce zero or more signals.

        Returning [] when nothing changed is the normal case — most polls
        produce no signals.
        """
```

**Adapters Phase 3**: `GMBListingSource`, `WebsiteHomepageSource`,
`WebsiteCareersSource`, `HiringIndeedSource`.
**Adapters Phase 8**: `LinkedInProfileSource`, `LinkedInCompanySource`,
`LinkedInPostsSource`.
**Adapters future**: `YelpSource`, `NewsMentionsSource`,
`InstagramProfileSource`, etc.

---

## LLMProvider Interface (BYO AI)

```python
class LLMProvider(Protocol):
    name: str

    async def complete(
        self,
        *,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        json_mode: bool = False,
        model: str,
    ) -> LLMResponse:
        """Universal completion call.

        Returns: LLMResponse(content, tokens_in, tokens_out, cost_usd,
                             model_used, provider, latency_ms)
        """
```

**Provider adapters Phase 4**:
- `AnthropicProvider` (and `AAMPDefaultProvider` which is `AnthropicProvider`
  using AAMP's key)
- `OpenAIProvider`
- `OpenRouterProvider`
- `GoogleGeminiProvider`

**Provider adapters future**:
- `OllamaProvider` (self-hosted)
- `vLLMProvider`
- `NIMProvider`
- `BedrockProvider`
- `AzureOpenAIProvider`
- `TogetherProvider`, `FireworksProvider`, `GroqProvider`

**Cost accounting flow**:
1. `LLMProvider.complete()` returns `cost_usd` based on provider's published
   pricing for `model_used`
2. Caller persists `ai_decisions.cost_usd`
3. After persisting, increment `engagements.monthly_ai_cost_usd` and
   `tenant_ai_config.current_month_spent_usd`
4. Pre-call check: if `monthly_ai_cost_usd > per_engagement_budget_usd`,
   pause engagement. If `tenant.current_month_spent_usd > monthly_budget_usd`,
   pause all engagements for tenant.

**Prompt portability constraint**:
- No tool-use / function-calling in core decision loop (varies across
  providers). Use structured prompts + JSON output instead.
- No Claude-specific constructs (artifacts, computer use, vision-only
  features) in core decisions. Image-handling is a separate provider feature
  flag.
- Every prompt must be tested against at least one non-Claude provider
  (DeepSeek V3 via OpenRouter as the cheap-target baseline).

---

## AI Decision Flow

The decision_maker worker makes one of 13 decision types. Each is a function
mapping `(engagement_context, signal | scheduled_check) → choice`.

**Decision types ranked by frequency (most → least common)**:

1. `score_signal_relevance` — cheap model, every new signal, 0–100 score + 1-line summary
2. `classify_reply` — cheap model, every email/SMS reply, intent label
3. `generate_engagement_summary` — medium model, weekly per engagement, paragraph summary
4. `select_next_step` — medium model, when playbook has multiple branches
5. `generate_content` — medium model, when `ai_personalization_mode='generated_from_context'`
6. `what_to_send` — expensive model, when high-relevance signal triggers
7. `when_to_send` — same call as what_to_send usually
8. `draft_reply` — expensive model, when BDR clicks "draft AI reply"
9. `recommend_playbook_switch` — expensive model, monthly check per engagement
10. `recommend_phase_transition` — expensive model, after phase-relevant signals
11. `recommend_tier_change` — cheap model, when engagement score moves significantly
12. `recommend_pause` — cheap model, when fatigue signals detected
13. `detect_fatigue` — cheap model, runs in batch nightly across all engagements

**Per-decision-type model selection** (via `tenant_ai_config`):
- Decisions 1, 2, 11, 12, 13 → `model_signal_scoring` (cheap, fast)
- Decisions 3, 4, 5 → `model_content_generation` (medium)
- Decisions 6, 7, 8, 9, 10 → `model_decision_making` (expensive)

---

## Cost Model

Token estimates per decision type (rough, will be measured in Phase 4):

| Decision | Tokens in | Tokens out | Frequency per engagement/mo |
|---|---|---|---|
| score_signal_relevance | ~800 | ~80 | 30 |
| classify_reply | ~600 | ~50 | 2 |
| generate_engagement_summary | ~2000 | ~200 | 4 |
| select_next_step | ~1500 | ~150 | 5 |
| generate_content | ~3000 | ~600 | 8 |
| what_to_send | ~4000 | ~800 | 3 |
| recommend_phase_transition | ~3000 | ~300 | 0.5 |
| recommend_playbook_switch | ~3500 | ~400 | 0.3 |
| draft_reply | ~3500 | ~600 | 0.5 |
| detect_fatigue (batched) | ~500 | ~50 | 4 |

### Cost scenarios per engagement per month

**A. Opus-everywhere** (current Claude Opus 4.8 pricing: $15/$75 per M tokens):
- Cheap calls ($15 in/$75 out): ~$0.10
- Medium calls: ~$0.40
- Expensive calls: ~$1.20
- **Total: ~$1.70/mo per engagement → $3,400/mo for 2000 engagements**

**B. Balanced** (Opus for 6,7,8,9,10; Sonnet for 3,4,5; Haiku for rest):
- Haiku ($1/$5 per M): ~$0.005
- Sonnet ($3/$15 per M): ~$0.075
- Opus: ~$1.20
- **Total: ~$1.28/mo per engagement → $2,560/mo for 2000 engagements**

**C. Cost-optimized** (DeepSeek V3 via OpenRouter for medium+expensive,
Haiku for cheap):
- DeepSeek V3 ($0.27/$1.10 per M): ~$0.20 total medium + expensive combined
- Haiku ($1/$5 per M) for cheap: ~$0.005
- **Total: ~$0.21/mo per engagement → $420/mo for 2000 engagements**

### Polling cost (Worker A)

GMB via Google Places API: ~$0.005 per poll × 30 polls/mo per engagement = $0.15/mo
Website scraping: compute-only, ~$0.001/mo
Hiring scraping: compute-only, ~$0.001/mo
**Total polling: ~$0.15/mo per engagement → $300/mo for 2000 engagements**

### Send cost (Worker C)

Resend: $0.40 per 1000 emails. ~10 emails/mo per engagement = $0.004
Twilio SMS: ~$0.0079 per SMS. ~2 SMS/mo per engagement = $0.016
**Total send: ~$0.02/mo per engagement → $40/mo for 2000 engagements**

### Total platform operating cost for BMP at 2000 active engagements

| Scenario | LLM | Polling | Send | **Total** |
|---|---|---|---|---|
| Opus-everywhere | $3,400 | $300 | $40 | **~$3,740/mo** |
| Balanced | $2,560 | $300 | $40 | **~$2,900/mo** |
| Cost-optimized | $420 | $300 | $40 | **~$760/mo** |

These are dramatically below my earlier $4-10K estimate because the LLM
cost-tiering pulled most of the load onto cheap models. The expensive
decisions (Tier 3 AI features) are infrequent. **BYO AI is the unlock that
makes this economically viable at any scale.**

---

## Rollback Story per Phase

| Phase | If we need to abort | Recovery time |
|---|---|---|
| 0 (design) | Throw away the design doc | minutes |
| 1 (schema) | Tables sit empty, no code references them | minutes |
| 2 (dispatcher) | Don't enable. Old engine continues. | minutes |
| 3 (signal watcher) | Stop the worker. No data loss (signals are append-only). | minutes |
| 4 (decision maker) | Stop the worker. Dispatcher only sends what was scheduled. | minutes |
| 5 (CRM UX) | New screens; don't break old screens. Hide via feature flag. | minutes |
| 6 (playbook editor) | Same. | minutes |
| 7 (cutover) | Old engine kept running in parallel 7+ days. Re-route new enrollments back. | <15 minutes |
| 8 (LinkedIn + Tier 3) | Disable per-tenant feature flag. | minutes |

**The 15-minute rule** applies across all phases: from any deployed state, we
can revert to "BMP's BDRs operate on the old system" in under 15 minutes.

---

## 5 Future-Features Stress Test

The schema must support these without breaking changes. If any requires
schema migration with DROP/breaking-ALTER, the schema needs revision.

### Test 1 — "Add WhatsApp channel"

**Steps**:
1. Implement `WhatsAppChannel` conforming to `ActionDispatcher` interface
2. Add `'whatsapp'` to `actions.channel` CHECK constraint (additive: ALTER
   TABLE actions DROP CONSTRAINT + re-ADD with new value — this is the only
   non-additive op needed, and it's safe because no existing data uses the
   new value)
3. Add `recipient_whatsapp` field to actions table (additive)
4. Wire `WhatsAppChannel` into `channel_registry`

**Verdict**: ✅ PASS. One additive column + one constraint expansion. No data
migration.

### Test 2 — "Add Yelp-review signal source"

**Steps**:
1. Implement `YelpListingSource` conforming to `SignalSource` interface
2. Add `'yelp_listing'` to `observations.source_type` CHECK constraint
3. Add `'yelp_review'` to `signals.signal_type` CHECK constraint
4. Wire `YelpListingSource` into `signal_source_registry`

**Verdict**: ✅ PASS. Two constraint expansions. No data migration.

### Test 3 — "Add account-based engagement (multiple contacts under one
company nurture)"

**Steps**:
1. Add `account_engagement_id` nullable FK column to `engagements` table
   (additive). When set, indicates this engagement is part of an
   account-based pursuit.
2. Add new table `account_engagements` (additive) — same shape as
   engagements but at company level
3. Update decision_maker to consider sibling engagements when deciding
4. UI surfaces account view aggregating multiple engagements

**Verdict**: ✅ PASS. Two additive constructs.

### Test 4 — "Add territory-based BDR routing"

**Steps**:
1. Add `territory` field to `engagements` table (additive)
2. Add `bdr_territories` table mapping users → territories (additive)
3. Auto-populate `assigned_bdr_id` based on territory at engagement creation

**Verdict**: ✅ PASS.

### Test 5 — "Tenant switches from Claude Opus to DeepSeek V3 via OpenRouter"

**Steps**:
1. Tenant updates `tenant_ai_config.provider = 'openrouter'`
2. Tenant updates `tenant_ai_config.api_key_encrypted` with their OpenRouter key
3. Tenant updates each `model_*` field to their chosen DeepSeek/Llama variants
4. Next time `decision_maker` makes a call, the provider lookup hits the
   OpenRouter adapter instead of Anthropic. All prompts continue to work
   because no tool-use / Claude-specific features in the core loop.
5. Cost accounting updates with OpenRouter pricing.

**Verdict**: ✅ PASS — provided we enforce the prompt-portability constraint
throughout Phase 4.

---

## Outstanding Open Questions

1. **LinkedIn signal-source provider**: scraping has legal + bot-detection
   risk. Options: Clay's licensed pipes, Phantombuster, Apollo data API, or
   accept "no LinkedIn signal" for v1. Defer decision to Phase 8 planning.

2. **Outcome polling vs webhooks**: Resend supports webhooks for opens/clicks
   — wire those up in Phase 2. Twilio supports webhooks for SMS replies —
   wire in Phase 2. LinkedIn: poll-only. Plan accordingly.

3. **Multi-contact handoff** (when Tim leaves Texas Remodel Team and Mike
   takes over): out of v1 scope. Tracked as future feature; schema supports
   it (engagement.status='terminal' on Tim, new engagement on Mike, both
   linked via `company_id`).

4. **BDR context journal**: how does the BDR feed informal context (call
   notes, side conversations) into AI decisions? v1 uses `engagement.notes`
   freeform. v2 might add a structured journal.

---

## Phase 1 Acceptance Criteria

Before Phase 2 begins, all of these must be TRUE:

- [ ] All 8 new tables created in prod via additive migration
- [ ] All CHECK constraints rejection-tested (verified at DB level)
- [ ] All UNIQUE idempotency keys collision-tested
- [ ] All FK constraints verified
- [ ] RLS policies applied to all 8 tables, cross-tenant query tested
- [ ] ORM models in `app/models.py` with no production code reading or writing
- [ ] Test suite covers every invariant (estimated 30+ integration tests)
- [ ] `LLMProvider`, `ActionDispatcher`, `SignalSource` interfaces defined
      (interfaces only, no implementations needed yet)
- [ ] Adversarial code-review run on schema + interfaces

---

## Phase 0 Closes With

- This document, reviewed by Steve
- Adversarial code-review of this document by a separate agent
- Steve's GO/NO-GO on advancing to Phase 1

If GO: I begin writing the Phase 1 migration scripts the same day.

If NO-GO: I revise this doc based on Steve's feedback. No code is written
until Phase 0 explicitly closes.
