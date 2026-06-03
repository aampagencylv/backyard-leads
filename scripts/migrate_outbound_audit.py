"""Outbound email audit log.

Every call to send_email() writes one row here, regardless of whether
the email reached Resend. Lets us answer "what did we send yesterday"
in one SQL query, surface anomalies in a daily digest, and triage
incidents like the 2026-06-03 Texas Remodel Team episode in 5 seconds
instead of an hour of forensic detective work.

Captures:
  - sender_user_id  : which BDR (or NULL for engine)
  - step_type       : email / call / linkedin / imessage / adhoc / internal
  - subject         : full subject (max 500 chars)
  - body_preview    : first 300 chars of the body that was about to go
  - recipient_email : the to_email
  - company_id      : tenant-scoped via company.tenant_id
  - status          : 'sent' | 'blocked' | 'failed' | 'transient'
  - blocked_reason  : populated when status='blocked' (which guard fired)
  - anomaly_score   : 0-100 scalar — see app/services/email_sender.py
  - resend_id       : the Resend message id when delivered
  - error_message   : on failure / transient
  - caller_module   : optional hint about which code path called send_email

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        # Create table if it doesn't exist
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS outbound_email_audit (
                id              SERIAL PRIMARY KEY,
                tenant_id       INTEGER,
                sender_user_id  INTEGER REFERENCES users(id),
                company_id      INTEGER,
                contact_id      INTEGER,
                email_id        INTEGER,
                step_type       VARCHAR(32),
                subject         VARCHAR(500),
                body_preview    TEXT,
                recipient_email VARCHAR(320),
                status          VARCHAR(20) NOT NULL,
                blocked_reason  VARCHAR(200),
                anomaly_score   SMALLINT DEFAULT 0,
                anomaly_flags   TEXT,
                resend_id       VARCHAR(80),
                error_message   TEXT,
                caller_module   VARCHAR(120),
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        print("+ table outbound_email_audit ensured")

        # Index for the daily-digest query (tenant + date range)
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_outbound_audit_tenant_created
            ON outbound_email_audit (tenant_id, created_at DESC)
        """))
        # Index for triage by status (e.g. "show me all blocked sends today")
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_outbound_audit_status_created
            ON outbound_email_audit (status, created_at DESC)
        """))
        # Index for sender_user query
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_outbound_audit_sender_created
            ON outbound_email_audit (sender_user_id, created_at DESC)
            WHERE sender_user_id IS NOT NULL
        """))
        print("+ indexes ensured")
    print("Migration complete — outbound_email_audit ready.")


if __name__ == "__main__":
    asyncio.run(main())
