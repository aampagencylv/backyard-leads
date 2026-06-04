"""Phase 1 of the sequence rebuild: the new schema.

Four NEW tables that REPLACE the polymorphic `generated_emails` (and
eventually the JSON-blob `sequence_templates`) once the migration
completes in Phase 5. They coexist additively for now — no prod code
reads or writes to these tables yet. Engine cutover happens later via
a separate commit. The `seq_*` prefix avoids collision with the legacy
`sequence_templates` table.

The design:

  seq_templates
    The recipe. "Default 30-Day Pool Builder Sequence." Has many steps.

  seq_template_steps
    Mutable, reorderable plan rows. Each step has channel + day_offset
    + subject_template + body_template. Editing a step touches THIS row
    only — never affects in-flight executions.

  seq_enrollments
    One row per (contact, template). The state machine: enrolled_at,
    status, current_step_index, next_due_at, paused_at, snooze_resume_at.
    UNIQUE (template_id, contact_id) on active rows — a contact can only
    be enrolled once at a time per template.

  seq_step_executions
    The immutable log. Every dispatch attempt creates one row with a
    UNIQUE idempotency_key. Captures the ACTUAL content sent (subject +
    body at render time), not a reference to the template — so if the
    template changes later, history doesn't drift.

Status enums (DB-enforced via CHECK constraints):

  seq_templates.is_active             bool
  seq_template_steps.channel          email | imessage | call | linkedin
  seq_template_steps.is_active        bool
  seq_enrollments.status              active | paused | snoozed | completed | replied | cancelled
  seq_step_executions.status          scheduled | sent | failed | skipped | transient | blocked

Why this fixes the Texas Remodel Team class of bug:

  - subject + body live on EXECUTION rows (captured at send time), not
    on a polymorphic step row that can be misread by another channel
  - idempotency_key UNIQUE prevents double-sends at the DB level
  - status is a single explicit field, not encoded across 5 nullable
    timestamps that need to be read together
  - editing the template doesn't touch in-flight executions

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        # ============================================================
        # seq_templates — the recipe
        # ============================================================
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seq_templates (
                id              SERIAL PRIMARY KEY,
                tenant_id       INTEGER,
                name            VARCHAR(200) NOT NULL,
                description     TEXT,
                is_default      BOOLEAN NOT NULL DEFAULT FALSE,
                is_active       BOOLEAN NOT NULL DEFAULT TRUE,
                created_by_user_id INTEGER REFERENCES users(id),
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_seq_templates_tenant
              ON seq_templates (tenant_id, is_active)
        """))
        await conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_seq_templates_one_default_per_tenant
              ON seq_templates (tenant_id) WHERE is_default = TRUE
        """))

        # ============================================================
        # seq_template_steps — the steps in the recipe
        # ============================================================
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seq_template_steps (
                id              SERIAL PRIMARY KEY,
                template_id     INTEGER NOT NULL REFERENCES seq_templates(id) ON DELETE CASCADE,
                tenant_id       INTEGER,
                step_order      INTEGER NOT NULL,
                channel         VARCHAR(20) NOT NULL
                                  CHECK (channel IN ('email','imessage','call','linkedin')),
                day_offset_from_enroll INTEGER NOT NULL DEFAULT 0,
                step_label      VARCHAR(60),
                subject_template TEXT,
                body_template   TEXT,
                skip_conditions_json TEXT,
                auto_execute    BOOLEAN NOT NULL DEFAULT TRUE,
                is_active       BOOLEAN NOT NULL DEFAULT TRUE,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_seq_template_steps_order
              ON seq_template_steps (template_id, step_order)
              WHERE is_active = TRUE
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_seq_template_steps_template
              ON seq_template_steps (template_id, step_order)
        """))

        # ============================================================
        # seq_enrollments — one row per (contact, template)
        # ============================================================
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seq_enrollments (
                id              SERIAL PRIMARY KEY,
                tenant_id       INTEGER,
                template_id     INTEGER NOT NULL REFERENCES seq_templates(id),
                contact_id      INTEGER NOT NULL REFERENCES contacts(id),
                company_id      INTEGER NOT NULL REFERENCES companies(id),
                status          VARCHAR(20) NOT NULL DEFAULT 'active'
                                  CHECK (status IN ('active','paused','snoozed','completed','replied','cancelled')),
                current_step_index INTEGER NOT NULL DEFAULT 0,
                next_due_at     TIMESTAMPTZ,
                enrolled_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                enrolled_by_user_id INTEGER REFERENCES users(id),
                paused_at       TIMESTAMPTZ,
                paused_reason   VARCHAR(200),
                snooze_resume_at TIMESTAMPTZ,
                snooze_reason   VARCHAR(200),
                snooze_set_by_user_id INTEGER REFERENCES users(id),
                reply_received_at TIMESTAMPTZ,
                reply_pause_reason VARCHAR(200),
                completed_at    TIMESTAMPTZ,
                completion_reason VARCHAR(100),
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_seq_enrollments_contact_template
              ON seq_enrollments (template_id, contact_id)
              WHERE status NOT IN ('completed','cancelled')
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_seq_enrollments_due
              ON seq_enrollments (status, next_due_at)
              WHERE status = 'active'
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_seq_enrollments_company
              ON seq_enrollments (company_id, status)
        """))

        # ============================================================
        # seq_step_executions — immutable log of every attempt
        # ============================================================
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seq_step_executions (
                id              SERIAL PRIMARY KEY,
                tenant_id       INTEGER,
                enrollment_id   INTEGER NOT NULL REFERENCES seq_enrollments(id),
                template_step_id INTEGER NOT NULL REFERENCES seq_template_steps(id),
                attempt_n       INTEGER NOT NULL DEFAULT 1,
                idempotency_key VARCHAR(200) NOT NULL,
                status          VARCHAR(20) NOT NULL DEFAULT 'scheduled'
                                  CHECK (status IN ('scheduled','sent','failed','skipped','transient','blocked')),
                channel         VARCHAR(20) NOT NULL,
                scheduled_at    TIMESTAMPTZ NOT NULL,
                executed_at     TIMESTAMPTZ,
                subject         VARCHAR(500),
                body            TEXT,
                recipient_email VARCHAR(320),
                recipient_phone VARCHAR(40),
                sent_by_user_id INTEGER REFERENCES users(id),
                resend_message_id VARCHAR(80),
                twilio_call_sid VARCHAR(80),
                blooio_message_id VARCHAR(80),
                error_message   TEXT,
                skip_reason     VARCHAR(80),
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        # THE structural fix for double-sends. Idempotency key is
        # f"{enrollment_id}-{template_step_id}-{attempt_n}" — second
        # INSERT with the same key fails the constraint at the DB,
        # so even a code bug that calls send twice gets stopped here.
        await conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_seq_step_executions_idem
              ON seq_step_executions (idempotency_key)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_seq_step_executions_enrollment
              ON seq_step_executions (enrollment_id, scheduled_at DESC)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_seq_step_executions_status
              ON seq_step_executions (status, scheduled_at)
              WHERE status IN ('scheduled','transient')
        """))

        print("+ seq_templates / seq_template_steps / seq_enrollments / seq_step_executions ensured")
    print("Migration complete — sequence v2 schema ready (additive; no prod impact).")


if __name__ == "__main__":
    asyncio.run(main())
