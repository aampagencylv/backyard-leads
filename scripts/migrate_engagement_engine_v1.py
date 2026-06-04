"""Phase 1 of the engagement engine rebuild — additive schema.

This is the foundation for the continuous lead nurture engine described in
docs/ENGAGEMENT_ENGINE_DESIGN.md (v3). It is purely additive: no production
code reads or writes these tables yet. Cutover happens in Phase 7.

Creates (all idempotent):
  4 lookup tables:
    - channel_types (SMALLINT PK)
    - signal_types (SMALLINT PK)
    - source_types (SMALLINT PK)
    - phase_transitions
  8 core domain tables:
    - engagements
    - playbooks
    - playbook_actions
    - signals
    - actions
    - ai_decisions  (partitioned monthly by created_at)
    - observations
    - tenant_ai_config
  2 email infrastructure tables:
    - email_identities
    - email_suppressions
  2 reply ingestion tables:
    - tenant_reply_inboxes
    - inbound_unattributed
  1 coordination table:
    - action_dedupe_counters
  6 trigger functions + trigger bindings:
    - enforce_action_recipient_matches_contact
    - enforce_tenant_consistency_via_engagement (signals, actions)
    - enforce_phase_transition
    - enforce_last_transition_by_set_on_phase_change
    - enforce_day_offset_mode_consistency
    - notify_lookup_change (on channel_types, signal_types, source_types)

Plus additive ALTERs to contacts, companies, tenants.

Why this is safe to run on prod:
  - additive only; no DROP / no breaking ALTER
  - all CREATE statements are IF NOT EXISTS
  - all ALTER ADD COLUMN are guarded
  - no production worker reads or writes any of these tables yet

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


# ──────────────────────────────────────────────────────────────────────────
# Lookup table seed data (must be stable — references in code rely on codes,
# not IDs, so adding a new code is safe; renaming or removing one is NOT)
# ──────────────────────────────────────────────────────────────────────────

CHANNEL_TYPES = [
    ("email",     "Email"),
    ("sms",       "SMS"),
    ("linkedin",  "LinkedIn message"),
    ("call_task", "BDR phone call task"),
    ("wait",      "Wait / no-op step"),
    ("manual",    "Manual BDR outreach"),
]

SIGNAL_TYPES = [
    # External (polled from prospect's online presence)
    ("linkedin_profile_change",    "LinkedIn profile change",         "external", 60),
    ("linkedin_post",              "LinkedIn post",                   "external", 50),
    ("linkedin_company_update",    "LinkedIn company update",         "external", 55),
    ("gmb_review",                 "Google My Business review",       "external", 70),
    ("gmb_post",                   "Google My Business post",         "external", 40),
    ("gmb_listing_change",         "GMB listing change",              "external", 65),
    ("website_change",             "Website content change",          "external", 50),
    ("website_new_page",           "Website new page",                "external", 60),
    ("hiring_signal",              "Hiring / new job posting",        "external", 75),
    ("press_mention",              "Press mention",                   "external", 80),
    ("news_mention",               "News mention",                    "external", 75),
    # Transport (events from our own outbound channels)
    ("email_open",                 "Email opened",                    "transport", 30),
    ("email_click",                "Email link clicked",              "transport", 60),
    ("email_reply",                "Email reply received",            "transport", 95),
    ("email_bounce",               "Email bounced",                   "transport", 40),
    ("email_complaint",            "Email marked as spam",            "transport", 85),
    ("email_unsubscribe",          "Email unsubscribe",               "transport", 90),
    ("sms_reply",                  "SMS reply received",              "transport", 95),
    ("sms_opt_out",                "SMS opt-out (STOP)",              "transport", 90),
    ("call_outcome",               "Call outcome logged",             "transport", 70),
    # Manual / lifecycle
    ("meeting_booked",             "Meeting booked",                  "manual",    95),
    ("meeting_completed",          "Meeting completed",               "manual",    90),
    ("meeting_no_show",            "Meeting no-show",                 "manual",    50),
    ("manual_note",                "BDR manual note",                 "manual",    40),
    ("contact_left_company",       "Contact left company",            "manual",    85),
    ("company_acquired",           "Company was acquired",            "manual",    80),
    ("company_closed",             "Company closed",                  "manual",    95),
    ("competitor_signed",          "Competitor signed",               "manual",    90),
    ("payment_failed",             "Payment failed (customer phase)", "manual",    85),
]

SOURCE_TYPES = [
    ("linkedin_profile",   "LinkedIn profile",       "engagement_engine.sources.linkedin.ProfileSource",       7),
    ("linkedin_company",   "LinkedIn company page",  "engagement_engine.sources.linkedin.CompanySource",      14),
    ("linkedin_posts",     "LinkedIn posts feed",    "engagement_engine.sources.linkedin.PostsSource",         7),
    ("gmb_listing",        "Google My Business",     "engagement_engine.sources.gmb.ListingSource",            7),
    ("website_homepage",   "Website homepage",       "engagement_engine.sources.website.HomepageSource",      14),
    ("website_careers",    "Website careers page",   "engagement_engine.sources.website.CareersSource",       14),
    ("hiring_indeed",      "Indeed hiring scan",     "engagement_engine.sources.hiring.IndeedSource",         14),
    ("hiring_glassdoor",   "Glassdoor hiring scan",  "engagement_engine.sources.hiring.GlassdoorSource",      14),
    ("news_mentions",      "News mentions",          "engagement_engine.sources.news.MentionsSource",          7),
    ("yelp_listing",       "Yelp listing",           "engagement_engine.sources.yelp.ListingSource",          30),
    ("facebook_page",      "Facebook page",          "engagement_engine.sources.facebook.PageSource",         14),
    ("instagram_profile",  "Instagram profile",      "engagement_engine.sources.instagram.ProfileSource",     14),
]

# Phase transitions — (from_phase, to_phase, allowed_by, requires_status).
# requires_status NULL means transition allowed regardless of status.
PHASE_TRANSITIONS = [
    # Normal forward flow
    ("cold_outreach",         "meeting_set",            "ai",     "active"),
    ("cold_outreach",         "meeting_set",            "bdr",    None),
    ("cold_outreach",         "declined",               "ai",     None),
    ("cold_outreach",         "declined",               "bdr",    None),
    ("cold_outreach",         "declined",               "system", None),
    ("cold_outreach",         "dormant",                "system", None),
    ("meeting_set",           "post_meeting_nurture",   "system", "active"),
    ("meeting_set",           "cold_outreach",          "bdr",    None),  # no-show reset
    ("post_meeting_nurture",  "qualified",              "ai",     "active"),
    ("post_meeting_nurture",  "qualified",              "bdr",    None),
    ("post_meeting_nurture",  "declined",               "ai",     None),
    ("post_meeting_nurture",  "declined",               "bdr",    None),
    ("qualified",             "customer",               "bdr",    None),
    ("qualified",             "declined",               "bdr",    None),
    ("customer",              "lost",                   "bdr",    None),  # churn
    # Re-engagement (creates new engagements.sequence_number)
    ("declined",              "cold_outreach",          "bdr",    None),
    ("dormant",               "cold_outreach",          "system", None),
    ("dormant",               "cold_outreach",          "bdr",    None),
]


async def main() -> None:
    async with engine.begin() as conn:
        await _create_lookup_tables(conn)
        await _seed_lookup_tables(conn)
        await _alter_existing_tables(conn)
        await _create_engagements(conn)
        await _create_playbooks(conn)
        await _create_playbook_actions(conn)
        await _create_observations(conn)
        await _create_signals(conn)
        await _create_ai_decisions(conn)
        await _create_actions(conn)
        await _add_cross_table_fks(conn)
        await _create_tenant_ai_config(conn)
        await _create_email_infrastructure(conn)
        await _create_reply_ingestion(conn)
        await _create_action_dedupe_counters(conn)
        await _create_trigger_functions(conn)
        await _create_triggers(conn)
        print("+ engagement engine v1 schema ensured (15 tables + 6 triggers + lookups seeded)")
    print("Migration complete — engagement engine v1 ready (additive; no prod impact).")


# ──────────────────────────────────────────────────────────────────────────
# Lookup tables (SMALLINT surrogate PKs per design rule #4 v3)
# ──────────────────────────────────────────────────────────────────────────

async def _create_lookup_tables(conn) -> None:
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS channel_types (
            id           SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            code         VARCHAR(20) NOT NULL UNIQUE,
            label        VARCHAR(60) NOT NULL,
            is_active    BOOLEAN NOT NULL DEFAULT TRUE,
            is_paused    BOOLEAN NOT NULL DEFAULT FALSE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS signal_types (
            id                 SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            code               VARCHAR(40) NOT NULL UNIQUE,
            label              VARCHAR(80) NOT NULL,
            category           VARCHAR(20) NOT NULL
                                 CHECK (category IN ('external','transport','manual')),
            default_relevance  SMALLINT CHECK (default_relevance BETWEEN 0 AND 100),
            is_active          BOOLEAN NOT NULL DEFAULT TRUE,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS source_types (
            id                 SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            code               VARCHAR(40) NOT NULL UNIQUE,
            label              VARCHAR(80) NOT NULL,
            adapter_class      VARCHAR(120) NOT NULL,
            default_poll_days  SMALLINT NOT NULL DEFAULT 7,
            is_active          BOOLEAN NOT NULL DEFAULT TRUE,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS phase_transitions (
            from_phase       VARCHAR(40) NOT NULL,
            to_phase         VARCHAR(40) NOT NULL,
            allowed_by       VARCHAR(20) NOT NULL
                               CHECK (allowed_by IN ('ai','bdr','system')),
            requires_status  VARCHAR(20),
            PRIMARY KEY (from_phase, to_phase, allowed_by)
        )
    """))


async def _seed_lookup_tables(conn) -> None:
    """Upsert lookup data. Codes are stable; new rows can be added safely."""
    for code, label in CHANNEL_TYPES:
        await conn.execute(text("""
            INSERT INTO channel_types (code, label)
            VALUES (:code, :label)
            ON CONFLICT (code) DO NOTHING
        """), {"code": code, "label": label})

    for code, label, category, default_rel in SIGNAL_TYPES:
        await conn.execute(text("""
            INSERT INTO signal_types (code, label, category, default_relevance)
            VALUES (:code, :label, :cat, :rel)
            ON CONFLICT (code) DO NOTHING
        """), {"code": code, "label": label, "cat": category, "rel": default_rel})

    for code, label, adapter_class, poll_days in SOURCE_TYPES:
        await conn.execute(text("""
            INSERT INTO source_types (code, label, adapter_class, default_poll_days)
            VALUES (:code, :label, :ac, :pd)
            ON CONFLICT (code) DO NOTHING
        """), {"code": code, "label": label, "ac": adapter_class, "pd": poll_days})

    for from_p, to_p, allowed_by, req_status in PHASE_TRANSITIONS:
        await conn.execute(text("""
            INSERT INTO phase_transitions (from_phase, to_phase, allowed_by, requires_status)
            VALUES (:f, :t, :a, :rs)
            ON CONFLICT (from_phase, to_phase, allowed_by) DO NOTHING
        """), {"f": from_p, "t": to_p, "a": allowed_by, "rs": req_status})


# ──────────────────────────────────────────────────────────────────────────
# Additive ALTERs to existing tables
# ──────────────────────────────────────────────────────────────────────────

async def _alter_existing_tables(conn) -> None:
    # contacts
    await conn.execute(text("""
        ALTER TABLE contacts ADD COLUMN IF NOT EXISTS timezone VARCHAR(50)
    """))
    await conn.execute(text("""
        ALTER TABLE contacts ADD COLUMN IF NOT EXISTS do_not_contact BOOLEAN
            NOT NULL DEFAULT FALSE
    """))
    # outreach_owner uses CHECK constraint added in two steps so it can be
    # extended later without a breaking ALTER. New tenants/contacts default
    # to 'legacy' so the old engine continues serving them until cutover.
    await conn.execute(text("""
        ALTER TABLE contacts ADD COLUMN IF NOT EXISTS outreach_owner VARCHAR(20)
            NOT NULL DEFAULT 'legacy'
    """))
    # Add CHECK only if absent (Postgres has no IF NOT EXISTS on constraints,
    # so we use a DO block).
    await conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'chk_contacts_outreach_owner'
            ) THEN
                ALTER TABLE contacts ADD CONSTRAINT chk_contacts_outreach_owner
                CHECK (outreach_owner IN (
                    'legacy','engagement_engine','none','paused','white_glove','disputed'
                ));
            END IF;
        END $$;
    """))

    # companies
    await conn.execute(text("""
        ALTER TABLE companies ADD COLUMN IF NOT EXISTS do_not_contact BOOLEAN
            NOT NULL DEFAULT FALSE
    """))
    await conn.execute(text("""
        ALTER TABLE companies ADD COLUMN IF NOT EXISTS do_not_contact_reason VARCHAR(200)
    """))
    await conn.execute(text("""
        ALTER TABLE companies ADD COLUMN IF NOT EXISTS do_not_contact_set_at TIMESTAMPTZ
    """))

    # tenants
    await conn.execute(text("""
        ALTER TABLE tenants ADD COLUMN IF NOT EXISTS reply_domain VARCHAR(253)
    """))
    await conn.execute(text("""
        ALTER TABLE tenants ADD COLUMN IF NOT EXISTS default_timezone VARCHAR(50)
            NOT NULL DEFAULT 'America/New_York'
    """))


# ──────────────────────────────────────────────────────────────────────────
# Core domain tables
# ──────────────────────────────────────────────────────────────────────────

async def _create_engagements(conn) -> None:
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS engagements (
            id                          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id                   INTEGER NOT NULL REFERENCES tenants(id),
            contact_id                  INTEGER NOT NULL REFERENCES contacts(id),
            company_id                  INTEGER NOT NULL REFERENCES companies(id),
            sequence_number             INTEGER NOT NULL DEFAULT 1,

            current_phase               VARCHAR(40) NOT NULL DEFAULT 'cold_outreach'
                                          CHECK (current_phase IN (
                                            'cold_outreach','meeting_set','post_meeting_nurture',
                                            'qualified','customer','declined','lost','dormant'
                                          )),
            last_transition_by          VARCHAR(20) NOT NULL DEFAULT 'system',
            status                      VARCHAR(20) NOT NULL DEFAULT 'active'
                                          CHECK (status IN ('active','paused','hibernating','terminal')),
            terminal_reason             VARCHAR(60),
            terminal_at                 TIMESTAMPTZ,

            current_playbook_id         INTEGER,
            current_playbook_version    INTEGER,
            current_action_index        INTEGER NOT NULL DEFAULT 0,

            next_action_due_at          TIMESTAMPTZ,
            last_outreach_at            TIMESTAMPTZ,
            last_signal_at              TIMESTAMPTZ,
            last_reply_at               TIMESTAMPTZ,

            assigned_bdr_id             INTEGER REFERENCES users(id),
            engagement_score            INTEGER NOT NULL DEFAULT 50
                                          CHECK (engagement_score BETWEEN 0 AND 100),
            engagement_score_updated_by VARCHAR(20)
                                          CHECK (engagement_score_updated_by IS NULL
                                                 OR engagement_score_updated_by IN
                                                    ('ai_decision','rule_engine','bdr')),
            engagement_score_updated_at TIMESTAMPTZ,
            tier                        VARCHAR(10) NOT NULL DEFAULT 'warm'
                                          CHECK (tier IN ('hot','warm','cold','dormant')),

            ai_engagement_summary       TEXT,
            summary_version             INTEGER NOT NULL DEFAULT 0,
            summary_updated_at          TIMESTAMPTZ,
            summary_stale_at            TIMESTAMPTZ,

            notes                       TEXT,

            monthly_ai_cost_usd         NUMERIC(10,4) NOT NULL DEFAULT 0,
            monthly_ai_cost_reset_at    TIMESTAMPTZ NOT NULL DEFAULT date_trunc('month', NOW()),

            started_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT chk_engagements_terminal_pairing CHECK (
                (status = 'terminal') = (terminal_at IS NOT NULL)
                AND (status = 'terminal') = (terminal_reason IS NOT NULL)
            ),
            CONSTRAINT chk_engagements_customer_state CHECK (
                current_phase != 'customer' OR status IN ('active','paused')
            )
        )
    """))

    await conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_engagements_contact_sequence
          ON engagements (contact_id, sequence_number)
    """))
    await conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_engagements_due
          ON engagements (status, next_action_due_at) WHERE status = 'active'
    """))
    await conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_engagements_tenant_phase
          ON engagements (tenant_id, current_phase, status)
    """))
    await conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_engagements_summary_stale
          ON engagements (summary_stale_at) WHERE summary_stale_at IS NOT NULL
    """))


async def _create_playbooks(conn) -> None:
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS playbooks (
            id                     SERIAL PRIMARY KEY,
            tenant_id              INTEGER REFERENCES tenants(id),
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
            duration_max_days      INTEGER,
            ai_strategy_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
            legacy_seq_template_id INTEGER REFERENCES seq_templates(id),
            is_active              BOOLEAN NOT NULL DEFAULT TRUE,
            version                INTEGER NOT NULL DEFAULT 1,
            parent_playbook_id     INTEGER REFERENCES playbooks(id),
            created_by_user_id     INTEGER REFERENCES users(id),
            created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    await conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_playbooks_tenant_phase
          ON playbooks (tenant_id, phase, is_active)
    """))
    # Now wire engagements.current_playbook_id FK (deferred because of mutual
    # creation order)
    await conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_engagements_current_playbook'
            ) THEN
                ALTER TABLE engagements ADD CONSTRAINT fk_engagements_current_playbook
                FOREIGN KEY (current_playbook_id) REFERENCES playbooks(id);
            END IF;
        END $$;
    """))


async def _create_playbook_actions(conn) -> None:
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS playbook_actions (
            id                       SERIAL PRIMARY KEY,
            playbook_id              INTEGER NOT NULL REFERENCES playbooks(id) ON DELETE CASCADE,
            tenant_id                INTEGER REFERENCES tenants(id),
            action_order             INTEGER NOT NULL,
            channel_id               SMALLINT NOT NULL REFERENCES channel_types(id),
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
            day_offset               INTEGER,
            skip_conditions_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
            legacy_seq_step_id       INTEGER REFERENCES seq_template_steps(id),
            is_active                BOOLEAN NOT NULL DEFAULT TRUE,
            created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    await conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_playbook_actions_order
          ON playbook_actions (playbook_id, action_order) WHERE is_active = TRUE
    """))


async def _create_observations(conn) -> None:
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS observations (
            id                     SERIAL PRIMARY KEY,
            tenant_id              INTEGER NOT NULL REFERENCES tenants(id),
            contact_id             INTEGER NOT NULL REFERENCES contacts(id),
            company_id             INTEGER REFERENCES companies(id),
            current_engagement_id  BIGINT,  -- FK added later (engagements created above; ok)
            source_type_id         SMALLINT NOT NULL REFERENCES source_types(id),
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
        )
    """))
    await conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_observations_engagement'
            ) THEN
                ALTER TABLE observations ADD CONSTRAINT fk_observations_engagement
                FOREIGN KEY (current_engagement_id) REFERENCES engagements(id);
            END IF;
        END $$;
    """))
    await conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_observations_contact_source
          ON observations (contact_id, source_type_id) WHERE is_active = TRUE
    """))
    await conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_observations_due
          ON observations (next_poll_at, is_active) WHERE is_active = TRUE
    """))


async def _create_signals(conn) -> None:
    # Signals are HIGH volume; use BIGINT IDENTITY per design rule #4.
    # We do NOT partition signals in v1 (yet) because they're already indexed
    # by engagement_id; partitioning is a Phase 3 follow-on if needed.
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS signals (
            id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id              INTEGER NOT NULL REFERENCES tenants(id),
            engagement_id          BIGINT NOT NULL REFERENCES engagements(id),
            contact_id             INTEGER NOT NULL REFERENCES contacts(id),
            signal_type_id         SMALLINT NOT NULL REFERENCES signal_types(id),
            source_url             TEXT,
            source_endpoint        VARCHAR(80),
            raw_data_json          JSONB NOT NULL,
            raw_data_hash          VARCHAR(64),
            is_untrusted_content   BOOLEAN NOT NULL DEFAULT TRUE,
            observed_at            TIMESTAMPTZ NOT NULL,
            detected_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            relevance_score        INTEGER CHECK (relevance_score BETWEEN 0 AND 100),
            ai_summary             TEXT,
            ai_scored_by_model     VARCHAR(60),
            ai_scoring_cost_usd    NUMERIC(8,5),
            triggered_action_id    BIGINT,  -- FK added after actions exists
            idempotency_key        VARCHAR(200) NOT NULL,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    await conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_signals_idempotency
          ON signals (idempotency_key)
    """))
    await conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_signals_engagement
          ON signals (engagement_id, detected_at DESC)
    """))
    await conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_signals_unscored
          ON signals (relevance_score) WHERE relevance_score IS NULL
    """))
    await conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_signals_high_relevance
          ON signals (engagement_id, relevance_score DESC, detected_at DESC)
          WHERE relevance_score >= 70
    """))


async def _create_ai_decisions(conn) -> None:
    # PARTITIONED BY RANGE (created_at). Partition column must be part of the
    # PK and any UNIQUE constraints. We use composite (id, created_at) for PK
    # and (idempotency_key, created_at) for uniqueness — duplicate idempotency
    # keys across months are not deduped, which is fine because decisions for
    # different months are semantically separate calls.
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS ai_decisions (
            id                       BIGINT GENERATED ALWAYS AS IDENTITY,
            tenant_id                INTEGER NOT NULL REFERENCES tenants(id),
            engagement_id            BIGINT NOT NULL,
            signal_id                BIGINT,
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
            estimated_cost_usd       NUMERIC(8,5),
            latency_ms               INTEGER,
            json_parse_attempts      INTEGER NOT NULL DEFAULT 1,
            json_parse_succeeded     BOOLEAN NOT NULL DEFAULT TRUE,
            fallback_provider_used   VARCHAR(40),
            output_validation_passed BOOLEAN NOT NULL DEFAULT TRUE,
            output_validation_errors JSONB,
            human_override_action_id BIGINT,
            idempotency_key          VARCHAR(200) NOT NULL,
            created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
    """))
    # Create partitions for the current + next 2 months. Subsequent months
    # are created by a maintenance job (Phase 4 deliverable).
    await _ensure_ai_decisions_partition(conn, months_ahead=0)
    await _ensure_ai_decisions_partition(conn, months_ahead=1)
    await _ensure_ai_decisions_partition(conn, months_ahead=2)
    # Idempotency UNIQUE must include the partition key (created_at).
    await conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_decisions_idempotency
          ON ai_decisions (idempotency_key, created_at)
    """))
    await conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_ai_decisions_engagement
          ON ai_decisions (engagement_id, created_at DESC)
    """))


async def _ensure_ai_decisions_partition(conn, months_ahead: int) -> None:
    """Create a monthly partition table for ai_decisions, idempotent."""
    # INTERVAL multiplication avoids the string-concat type ambiguity that
    # bites bound integer parameters under asyncpg.
    sql = text("""
        WITH bounds AS (
            SELECT
                date_trunc('month', NOW() + (:m * INTERVAL '1 month')) AS start_dt,
                date_trunc('month', NOW() + (:m_plus_1 * INTERVAL '1 month')) AS end_dt
        )
        SELECT
            'ai_decisions_' || to_char(start_dt, 'YYYY_MM') AS partition_name,
            to_char(start_dt, 'YYYY-MM-DD') AS start_iso,
            to_char(end_dt, 'YYYY-MM-DD') AS end_iso
        FROM bounds
    """)
    result = (await conn.execute(sql, {"m": months_ahead,
                                       "m_plus_1": months_ahead + 1})).first()
    partition_name = result.partition_name
    start_iso = result.start_iso
    end_iso = result.end_iso
    await conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {partition_name}
        PARTITION OF ai_decisions
        FOR VALUES FROM ('{start_iso}') TO ('{end_iso}')
    """))


async def _create_actions(conn) -> None:
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS actions (
            id                       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id                INTEGER NOT NULL REFERENCES tenants(id),
            engagement_id            BIGINT NOT NULL REFERENCES engagements(id),
            contact_id               INTEGER NOT NULL REFERENCES contacts(id),
            playbook_action_id       INTEGER REFERENCES playbook_actions(id),
            triggered_by_signal_id   BIGINT REFERENCES signals(id),
            triggered_by_decision_id BIGINT,  -- partitioned table; cross-FK not supported

            channel_id               SMALLINT NOT NULL REFERENCES channel_types(id),
            status                   VARCHAR(20) NOT NULL DEFAULT 'scheduled'
                                       CHECK (status IN (
                                         'scheduled','sent','failed','skipped',
                                         'completed','blocked','awaiting_approval'
                                       )),
            requires_human_review    BOOLEAN NOT NULL DEFAULT FALSE,
            approved_by_user_id      INTEGER REFERENCES users(id),
            approved_at              TIMESTAMPTZ,

            scheduled_at             TIMESTAMPTZ NOT NULL,
            local_scheduled_at       TIMESTAMP,
            contact_timezone         VARCHAR(50),
            executed_at              TIMESTAMPTZ,

            stale_after              TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '24 hours'),
            supersedes_action_id     BIGINT REFERENCES actions(id),
            superseded_by_action_id  BIGINT REFERENCES actions(id),

            subject                  VARCHAR(500),
            body                     TEXT,
            task_description         TEXT,
            recipient_email          VARCHAR(320),
            recipient_phone          VARCHAR(40),
            recipient_linkedin_url   VARCHAR(500),

            idempotency_key          VARCHAR(200) NOT NULL,
            external_id              VARCHAR(120),
            dispatch_heartbeat_at    TIMESTAMPTZ,
            dispatch_worker_id       VARCHAR(40),
            error_message            TEXT,
            skip_reason              VARCHAR(80),
            outcome                  VARCHAR(40),
            outcome_observed_at      TIMESTAMPTZ,

            ai_strategy_used         VARCHAR(40),
            ai_generation_cost_usd   NUMERIC(8,5),
            send_cost_usd            NUMERIC(8,5),
            sent_by_user_id          INTEGER REFERENCES users(id),

            created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    await conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_actions_idempotency
          ON actions (idempotency_key)
    """))
    await conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_actions_scheduled
          ON actions (status, scheduled_at)
          WHERE status IN ('scheduled','awaiting_approval')
    """))
    await conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_actions_engagement
          ON actions (engagement_id, scheduled_at DESC)
    """))


async def _add_cross_table_fks(conn) -> None:
    """Add FKs that couldn't be created at table-creation time due to
    mutual dependencies (signals ↔ actions, ai_decisions ↔ actions)."""
    await conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_signals_triggered_action'
            ) THEN
                ALTER TABLE signals ADD CONSTRAINT fk_signals_triggered_action
                FOREIGN KEY (triggered_action_id) REFERENCES actions(id);
            END IF;
        END $$;
    """))
    # ai_decisions ↔ actions intentionally not FK-constrained: ai_decisions is
    # partitioned and cross-partition FKs are not supported by Postgres.
    # human_override_action_id is enforced at application layer.


async def _create_tenant_ai_config(conn) -> None:
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS tenant_ai_config (
            tenant_id                      INTEGER PRIMARY KEY REFERENCES tenants(id),
            provider                       VARCHAR(40) NOT NULL DEFAULT 'aamp_default'
                                             CHECK (provider IN (
                                               'aamp_default','anthropic','openai','openrouter',
                                               'google_gemini','ollama','vllm','nim',
                                               'bedrock','azure_openai','together','fireworks','groq'
                                             )),
            api_key_kms_arn                VARCHAR(200),
            api_key_encrypted              TEXT,
            api_key_last_validated_at      TIMESTAMPTZ,
            api_key_last_error             TEXT,
            base_url                       VARCHAR(200),
            model_signal_scoring           VARCHAR(80) NOT NULL DEFAULT 'claude-haiku-4-5',
            model_reply_classification     VARCHAR(80) NOT NULL DEFAULT 'claude-haiku-4-5',
            model_content_generation       VARCHAR(80) NOT NULL DEFAULT 'claude-sonnet-4-6',
            model_decision_making          VARCHAR(80) NOT NULL DEFAULT 'claude-opus-4-7',
            model_engagement_summary       VARCHAR(80) NOT NULL DEFAULT 'claude-sonnet-4-6',
            monthly_budget_usd             NUMERIC(10,2),
            per_engagement_budget_usd      NUMERIC(8,4) NOT NULL DEFAULT 5.00,
            fallback_provider              VARCHAR(40),
            dedupe_email_per_day           INTEGER NOT NULL DEFAULT 1,
            dedupe_sms_per_day             INTEGER NOT NULL DEFAULT 1,
            dedupe_linkedin_per_day        INTEGER NOT NULL DEFAULT 1,
            tcpa_b2b_override              BOOLEAN NOT NULL DEFAULT FALSE,
            default_timezone               VARCHAR(50) NOT NULL DEFAULT 'America/New_York',
            current_month_spent_usd        NUMERIC(10,4) NOT NULL DEFAULT 0,
            current_month_reset_at         TIMESTAMPTZ NOT NULL DEFAULT date_trunc('month', NOW()),
            created_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT chk_provider_api_key CHECK (
                (provider = 'aamp_default'
                  AND api_key_encrypted IS NULL
                  AND api_key_kms_arn IS NULL)
                OR
                (provider != 'aamp_default'
                  AND ((api_key_encrypted IS NOT NULL AND api_key_kms_arn IS NULL)
                       OR (api_key_encrypted IS NULL AND api_key_kms_arn IS NOT NULL)))
            )
        )
    """))


async def _create_email_infrastructure(conn) -> None:
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS email_identities (
            id                    SERIAL PRIMARY KEY,
            tenant_id             INTEGER NOT NULL REFERENCES tenants(id),
            sender_email          VARCHAR(320) NOT NULL,
            sender_name           VARCHAR(200),
            domain                VARCHAR(253) NOT NULL,
            spf_verified          BOOLEAN NOT NULL DEFAULT FALSE,
            dkim_verified         BOOLEAN NOT NULL DEFAULT FALSE,
            dmarc_policy          VARCHAR(20),
            warmup_stage          VARCHAR(20) NOT NULL DEFAULT 'new'
                                    CHECK (warmup_stage IN (
                                      'new','week1','week2','week3','week4','warm','paused'
                                    )),
            daily_send_cap        INTEGER NOT NULL DEFAULT 50,
            sent_today            INTEGER NOT NULL DEFAULT 0,
            sent_today_date       DATE NOT NULL DEFAULT CURRENT_DATE,
            reset_timezone        VARCHAR(50) NOT NULL DEFAULT 'America/New_York',
            bounce_rate_24h       NUMERIC(5,4),
            complaint_rate_24h    NUMERIC(5,4),
            last_validated_at     TIMESTAMPTZ,
            is_active             BOOLEAN NOT NULL DEFAULT TRUE,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    await conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_email_identities_sender
          ON email_identities (tenant_id, sender_email)
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS email_suppressions (
            id                  SERIAL PRIMARY KEY,
            tenant_id           INTEGER NOT NULL REFERENCES tenants(id),
            recipient_email     VARCHAR(320) NOT NULL,
            reason              VARCHAR(40) NOT NULL
                                  CHECK (reason IN (
                                    'hard_bounce','complaint','unsubscribe','manual','spam_trap'
                                  )),
            source              VARCHAR(40),
            suppressed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at          TIMESTAMPTZ,
            is_currently_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    # Partial index uses IMMUTABLE column (is_currently_active), not NOW().
    # A separate scheduled job flips is_currently_active=FALSE when expired.
    await conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_email_suppressions_active
          ON email_suppressions (tenant_id, recipient_email)
          WHERE is_currently_active = TRUE
    """))


async def _create_reply_ingestion(conn) -> None:
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS tenant_reply_inboxes (
            id                       SERIAL PRIMARY KEY,
            tenant_id                INTEGER NOT NULL REFERENCES tenants(id),
            ingestion_mode           VARCHAR(20) NOT NULL DEFAULT 'webhook'
                                       CHECK (ingestion_mode IN ('webhook','imap')),
            reply_domain             VARCHAR(253) NOT NULL,
            imap_host                VARCHAR(253),
            imap_port                INTEGER,
            imap_user                VARCHAR(320),
            imap_password_kms_arn    VARCHAR(200),
            imap_password_encrypted  TEXT,
            last_poll_at             TIMESTAMPTZ,
            is_active                BOOLEAN NOT NULL DEFAULT TRUE,
            created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT chk_imap_password_storage CHECK (
                ingestion_mode != 'imap'
                OR (imap_password_encrypted IS NOT NULL AND imap_password_kms_arn IS NULL)
                OR (imap_password_encrypted IS NULL AND imap_password_kms_arn IS NOT NULL)
            ),
            CONSTRAINT chk_imap_fields_required CHECK (
                ingestion_mode != 'imap'
                OR (imap_host IS NOT NULL AND imap_port IS NOT NULL AND imap_user IS NOT NULL)
            )
        )
    """))
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS inbound_unattributed (
            id              SERIAL PRIMARY KEY,
            tenant_id       INTEGER REFERENCES tenants(id),
            envelope_from   VARCHAR(320),
            envelope_to     VARCHAR(320),
            subject         VARCHAR(998),
            cleaned_body    TEXT,
            raw_payload     JSONB,
            received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reviewed_at     TIMESTAMPTZ,
            reviewed_by_user_id INTEGER REFERENCES users(id),
            resolution      VARCHAR(40)
                              CHECK (resolution IS NULL OR resolution IN (
                                'attributed_manually','spam','out_of_office',
                                'unrelated','do_not_contact_request'
                              )),
            attributed_engagement_id BIGINT REFERENCES engagements(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))


async def _create_action_dedupe_counters(conn) -> None:
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS action_dedupe_counters (
            engagement_id  BIGINT NOT NULL REFERENCES engagements(id),
            channel_id     SMALLINT NOT NULL REFERENCES channel_types(id),
            date           DATE NOT NULL,
            count          INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (engagement_id, channel_id, date)
        )
    """))


# ──────────────────────────────────────────────────────────────────────────
# Trigger functions
# ──────────────────────────────────────────────────────────────────────────

async def _create_trigger_functions(conn) -> None:
    # 1. Recipient-lock trigger: actions.recipient_* must match engagement's
    # contact's email/phone/linkedin (Rule #12).
    await conn.execute(text("""
        CREATE OR REPLACE FUNCTION enforce_action_recipient_matches_contact()
        RETURNS TRIGGER AS $$
        DECLARE
            contact_email TEXT;
            contact_phone TEXT;
            contact_linkedin TEXT;
            channel_code TEXT;
        BEGIN
            -- Look up the channel code via the FK.
            SELECT code INTO channel_code FROM channel_types WHERE id = NEW.channel_id;

            -- Exempt manual channel + BDR-attributed actions.
            -- BDR may legitimately CC a different contact at same company.
            IF channel_code = 'manual' AND NEW.sent_by_user_id IS NOT NULL THEN
                RETURN NEW;
            END IF;

            SELECT c.email, c.phone, c.linkedin_url
            INTO contact_email, contact_phone, contact_linkedin
            FROM contacts c
            JOIN engagements e ON e.contact_id = c.id
            WHERE e.id = NEW.engagement_id;

            IF NEW.recipient_email IS NOT NULL
               AND NEW.recipient_email != contact_email THEN
                RAISE EXCEPTION
                    'action.recipient_email (%) does not match engagement contact (%)',
                    NEW.recipient_email, contact_email;
            END IF;
            IF NEW.recipient_phone IS NOT NULL
               AND NEW.recipient_phone != contact_phone THEN
                RAISE EXCEPTION
                    'action.recipient_phone (%) does not match engagement contact (%)',
                    NEW.recipient_phone, contact_phone;
            END IF;
            IF NEW.recipient_linkedin_url IS NOT NULL
               AND NEW.recipient_linkedin_url != contact_linkedin THEN
                RAISE EXCEPTION
                    'action.recipient_linkedin_url (%) does not match engagement contact (%)',
                    NEW.recipient_linkedin_url, contact_linkedin;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))

    # 2. Tenant consistency (signals + actions must share tenant with their
    # engagement).
    await conn.execute(text("""
        CREATE OR REPLACE FUNCTION enforce_tenant_consistency_via_engagement()
        RETURNS TRIGGER AS $$
        DECLARE
            engagement_tenant INTEGER;
        BEGIN
            SELECT tenant_id INTO engagement_tenant
            FROM engagements WHERE id = NEW.engagement_id;

            IF engagement_tenant IS NULL THEN
                RAISE EXCEPTION 'engagement % does not exist', NEW.engagement_id;
            END IF;

            IF NEW.tenant_id != engagement_tenant THEN
                RAISE EXCEPTION
                    'tenant_id (%) does not match engagement tenant (%)',
                    NEW.tenant_id, engagement_tenant;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))

    # 3. Phase transition FSM
    await conn.execute(text("""
        CREATE OR REPLACE FUNCTION enforce_phase_transition()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.current_phase = OLD.current_phase THEN
                RETURN NEW;
            END IF;

            IF NEW.last_transition_by = OLD.last_transition_by THEN
                RAISE EXCEPTION
                    'last_transition_by must be updated alongside current_phase';
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM phase_transitions
                WHERE from_phase = OLD.current_phase
                  AND to_phase = NEW.current_phase
                  AND allowed_by = NEW.last_transition_by
                  AND (requires_status IS NULL OR requires_status = NEW.status)
            ) THEN
                RAISE EXCEPTION
                    'illegal phase transition % -> % by % (status=%)',
                    OLD.current_phase, NEW.current_phase, NEW.last_transition_by, NEW.status;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))

    # 4. day_offset / mode coupling on playbook_actions
    await conn.execute(text("""
        CREATE OR REPLACE FUNCTION enforce_day_offset_mode_consistency()
        RETURNS TRIGGER AS $$
        DECLARE
            pb_mode TEXT;
        BEGIN
            SELECT mode INTO pb_mode FROM playbooks WHERE id = NEW.playbook_id;

            IF pb_mode = 'linear_sequence' AND NEW.day_offset IS NULL THEN
                RAISE EXCEPTION
                    'day_offset required when playbook mode is linear_sequence';
            END IF;
            IF pb_mode = 'signal_driven' AND NEW.day_offset IS NOT NULL THEN
                RAISE EXCEPTION
                    'day_offset must be NULL when playbook mode is signal_driven';
            END IF;
            -- hybrid + trigger_response: either allowed

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))

    # 5. LISTEN/NOTIFY on lookup-table changes (cache invalidation)
    await conn.execute(text("""
        CREATE OR REPLACE FUNCTION notify_lookup_change()
        RETURNS TRIGGER AS $$
        BEGIN
            PERFORM pg_notify('lookup_change', TG_TABLE_NAME);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))


async def _create_triggers(conn) -> None:
    # Each trigger binding wrapped in DO block for idempotency (CREATE TRIGGER
    # has no IF NOT EXISTS in older PG versions).

    # Recipient lock on actions
    await conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger WHERE tgname = 'trg_actions_recipient_lock'
            ) THEN
                CREATE TRIGGER trg_actions_recipient_lock
                BEFORE INSERT OR UPDATE ON actions
                FOR EACH ROW EXECUTE FUNCTION enforce_action_recipient_matches_contact();
            END IF;
        END $$;
    """))

    # Tenant consistency on signals + actions
    await conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger WHERE tgname = 'trg_signals_tenant_consistency'
            ) THEN
                CREATE TRIGGER trg_signals_tenant_consistency
                BEFORE INSERT OR UPDATE ON signals
                FOR EACH ROW EXECUTE FUNCTION enforce_tenant_consistency_via_engagement();
            END IF;
        END $$;
    """))
    await conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger WHERE tgname = 'trg_actions_tenant_consistency'
            ) THEN
                CREATE TRIGGER trg_actions_tenant_consistency
                BEFORE INSERT OR UPDATE ON actions
                FOR EACH ROW EXECUTE FUNCTION enforce_tenant_consistency_via_engagement();
            END IF;
        END $$;
    """))

    # Phase transition FSM
    await conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger WHERE tgname = 'trg_engagements_phase_transition'
            ) THEN
                CREATE TRIGGER trg_engagements_phase_transition
                BEFORE UPDATE ON engagements
                FOR EACH ROW EXECUTE FUNCTION enforce_phase_transition();
            END IF;
        END $$;
    """))

    # day_offset mode coupling
    await conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger WHERE tgname = 'trg_playbook_actions_mode_consistency'
            ) THEN
                CREATE TRIGGER trg_playbook_actions_mode_consistency
                BEFORE INSERT OR UPDATE ON playbook_actions
                FOR EACH ROW EXECUTE FUNCTION enforce_day_offset_mode_consistency();
            END IF;
        END $$;
    """))

    # LISTEN/NOTIFY on lookup tables
    for table in ("channel_types", "signal_types", "source_types"):
        await conn.execute(text(f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgname = 'trg_{table}_notify_change'
                ) THEN
                    CREATE TRIGGER trg_{table}_notify_change
                    AFTER INSERT OR UPDATE OR DELETE ON {table}
                    FOR EACH ROW EXECUTE FUNCTION notify_lookup_change();
                END IF;
            END $$;
        """))


if __name__ == "__main__":
    asyncio.run(main())
