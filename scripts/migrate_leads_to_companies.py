"""
Migrate leads-centric schema to companies / contacts / deals CRM.

Each Lead becomes:
  - one Company (same id, for FK continuity during the swap)
  - one primary Contact (if contact_* data present, else a blank placeholder
    when the lead already has generated emails — to preserve them)
  - one Deal (only when deal_value or non-default deal_stage is set)

Then activities, tasks, generated_emails, and lead_tags are repointed onto
companies / contacts. Old columns and tables are dropped.

Idempotent: safe to run on every app start. Each step inspects the schema
before acting.

Usage:
    python -m scripts.migrate_leads_to_companies
"""
from __future__ import annotations
import asyncio
import secrets
from sqlalchemy import text

from app.database import engine


# ============================================================
# Helpers
# ============================================================

async def _table_exists(conn, table: str) -> bool:
    rows = (await conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"
    ), {"t": table})).fetchall()
    return len(rows) > 0


async def _columns(conn, table: str) -> set[str]:
    if not await _table_exists(conn, table):
        return set()
    rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
    return {row[1] for row in rows}


# ============================================================
# Step 1: Create new tables
# ============================================================

CREATE_COMPANIES = """
CREATE TABLE IF NOT EXISTS companies (
    id              INTEGER PRIMARY KEY,
    search_id       INTEGER REFERENCES searches(id),
    name            VARCHAR(500) NOT NULL,
    phone           VARCHAR(50),
    website         VARCHAR(500),
    address         VARCHAR(500),
    city            VARCHAR(255),
    state           VARCHAR(100),
    rating          FLOAT,
    review_count    INTEGER,
    business_type   VARCHAR(255),
    enriched        BOOLEAN DEFAULT 0,
    site_speed_score FLOAT,
    has_blog        BOOLEAN,
    has_social_links BOOLEAN,
    last_review_date VARCHAR(100),
    mobile_friendly BOOLEAN,
    has_ssl         BOOLEAN,
    tech_stack      TEXT,
    problems_found  TEXT,
    enrichment_summary TEXT,
    status          VARCHAR(50) DEFAULT 'new',
    assigned_to     INTEGER REFERENCES users(id),
    email_generated BOOLEAN DEFAULT 0,
    email_sent      BOOLEAN DEFAULT 0,
    pushed_to_hubspot BOOLEAN DEFAULT 0,
    sequence_started_at DATETIME,
    linkedin_url    VARCHAR(500),
    created_at      DATETIME DEFAULT (datetime('now')),
    updated_at      DATETIME DEFAULT (datetime('now'))
)
"""

CREATE_CONTACTS = """
CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      INTEGER NOT NULL REFERENCES companies(id),
    first_name      VARCHAR(80) DEFAULT '',
    last_name       VARCHAR(80) DEFAULT '',
    title           VARCHAR(255),
    email           VARCHAR(255),
    phone           VARCHAR(50),
    linkedin_url    VARCHAR(500),
    is_primary      BOOLEAN DEFAULT 0,
    notes           TEXT,
    email_status    VARCHAR(20) DEFAULT 'unknown',
    unsubscribed_at DATETIME,
    unsubscribe_token VARCHAR(64),
    created_at      DATETIME DEFAULT (datetime('now')),
    updated_at      DATETIME DEFAULT (datetime('now'))
)
"""

CREATE_DEALS = """
CREATE TABLE IF NOT EXISTS deals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      INTEGER NOT NULL REFERENCES companies(id),
    name            VARCHAR(255) NOT NULL,
    value           FLOAT,
    stage           VARCHAR(50) DEFAULT 'prospecting',
    pipeline        VARCHAR(50) DEFAULT 'default',
    probability     INTEGER DEFAULT 0,
    expected_close_date DATETIME,
    closed_at       DATETIME,
    lost_reason     VARCHAR(255),
    assigned_to     INTEGER REFERENCES users(id),
    created_at      DATETIME DEFAULT (datetime('now')),
    updated_at      DATETIME DEFAULT (datetime('now'))
)
"""

CREATE_COMPANY_TAGS = """
CREATE TABLE IF NOT EXISTS company_tags (
    company_id      INTEGER NOT NULL REFERENCES companies(id),
    tag_id          INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (company_id, tag_id)
)
"""


# ============================================================
# Step 2: Add columns to existing tables
# ============================================================

NEW_ACTIVITY_COLS = [
    ("company_id", "INTEGER REFERENCES companies(id)"),
    ("contact_id", "INTEGER REFERENCES contacts(id)"),
    ("deal_id",    "INTEGER REFERENCES deals(id)"),
]
NEW_TASK_COLS = [
    ("company_id", "INTEGER REFERENCES companies(id)"),
    ("contact_id", "INTEGER REFERENCES contacts(id)"),
    ("deal_id",    "INTEGER REFERENCES deals(id)"),
]
NEW_EMAIL_COLS = [
    ("contact_id", "INTEGER REFERENCES contacts(id)"),
    ("company_id", "INTEGER REFERENCES companies(id)"),
    ("paused_at",  "DATETIME"),
]


# ============================================================
# Main
# ============================================================

async def main() -> None:
    async with engine.begin() as conn:
        # ----- Step 1: Create new tables -----
        await conn.execute(text(CREATE_COMPANIES))
        await conn.execute(text(CREATE_CONTACTS))
        await conn.execute(text(CREATE_DEALS))
        await conn.execute(text(CREATE_COMPANY_TAGS))
        print("Step 1: tables ensured (companies, contacts, deals, company_tags)")

        # ----- Step 2: Add columns to existing tables -----
        for table, cols in (
            ("activities",       NEW_ACTIVITY_COLS),
            ("tasks",            NEW_TASK_COLS),
            ("generated_emails", NEW_EMAIL_COLS),
        ):
            existing = await _columns(conn, table)
            if not existing:
                continue  # table doesn't exist (fresh install)
            for name, ddl in cols:
                if name not in existing:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    print(f"Step 2: + {table}.{name}")

        # ----- Step 3-8: Backfill from leads (only if leads table still exists) -----
        if not await _table_exists(conn, "leads"):
            print("No leads table — fresh install, skipping backfill")
            return

        leads_columns = await _columns(conn, "leads")
        # Only run backfill if companies is empty (idempotency)
        company_count = (await conn.execute(text("SELECT COUNT(*) FROM companies"))).scalar()
        if company_count == 0:
            # ----- Step 3: leads -> companies (preserve id) -----
            await conn.execute(text("""
                INSERT INTO companies (
                    id, search_id, name, phone, website, address, city, state,
                    rating, review_count, business_type,
                    enriched, site_speed_score, has_blog, has_social_links,
                    last_review_date, mobile_friendly, has_ssl, tech_stack,
                    problems_found, enrichment_summary,
                    status, assigned_to,
                    email_generated, email_sent, pushed_to_hubspot, sequence_started_at,
                    linkedin_url, created_at, updated_at
                )
                SELECT
                    id, search_id, business_name, phone, website, address, city, state,
                    rating, review_count, business_type,
                    enriched, site_speed_score, has_blog, has_social_links,
                    last_review_date, mobile_friendly, has_ssl, tech_stack,
                    problems_found, enrichment_summary,
                    status, assigned_to,
                    email_generated, email_sent, pushed_to_hubspot, sequence_started_at,
                    linkedin_url, created_at, updated_at
                FROM leads
            """))
            n = (await conn.execute(text("SELECT COUNT(*) FROM companies"))).scalar()
            print(f"Step 3: copied {n} lead(s) -> companies")

        # ----- Step 4: leads.contact_* -> contacts (idempotency: only if no contact for that company yet) -----
        leads_with_contact = (await conn.execute(text("""
            SELECT id, contact_name, contact_email, contact_title, contact_phone, contact_linkedin
            FROM leads
            WHERE id NOT IN (SELECT company_id FROM contacts)
              AND (
                  (contact_name  IS NOT NULL AND contact_name  != '')
               OR (contact_email IS NOT NULL AND contact_email != '')
               OR (contact_phone IS NOT NULL AND contact_phone != '')
              )
        """))).fetchall()
        for row in leads_with_contact:
            lead_id, name, email, title, phone, linkedin = row
            first, last = _split_name(name)
            await conn.execute(text("""
                INSERT INTO contacts (company_id, first_name, last_name, title, email, phone, linkedin_url, is_primary, unsubscribe_token)
                VALUES (:c, :f, :l, :t, :e, :p, :ln, 1, :tok)
            """), {
                "c": lead_id, "f": first, "l": last,
                "t": title or None, "e": (email or None),
                "p": phone or None, "ln": linkedin or None,
                "tok": secrets.token_urlsafe(24),
            })
        if leads_with_contact:
            print(f"Step 4: created {len(leads_with_contact)} primary contact(s) from lead contact_* fields")

        # ----- Step 4b: Leads with generated_emails but no contact yet -> placeholder contact -----
        if await _table_exists(conn, "generated_emails"):
            orphaned = (await conn.execute(text("""
                SELECT DISTINCT ge.lead_id FROM generated_emails ge
                LEFT JOIN contacts c ON c.company_id = ge.lead_id
                WHERE c.id IS NULL AND ge.lead_id IS NOT NULL
            """))).fetchall()
            for (lead_id,) in orphaned:
                await conn.execute(text("""
                    INSERT INTO contacts (company_id, first_name, last_name, is_primary, unsubscribe_token)
                    VALUES (:c, '', '', 1, :tok)
                """), {"c": lead_id, "tok": secrets.token_urlsafe(24)})
            if orphaned:
                print(f"Step 4b: created {len(orphaned)} placeholder contact(s) for leads with sequences")

        # ----- Step 5: leads w/ deal_value or non-default deal_stage -> deals -----
        if "deal_value" in leads_columns or "deal_stage" in leads_columns:
            deal_leads = (await conn.execute(text("""
                SELECT id, business_name, deal_value, deal_stage, assigned_to, created_at
                FROM leads
                WHERE id NOT IN (SELECT company_id FROM deals)
                  AND ((deal_value IS NOT NULL AND deal_value > 0)
                    OR (deal_stage IS NOT NULL AND deal_stage != '' AND deal_stage != 'prospect'))
            """))).fetchall()
            for lid, bname, value, stage, owner, created in deal_leads:
                # Map old "prospect" to new "prospecting"
                new_stage = "prospecting" if stage in (None, "", "prospect") else stage
                prob = _stage_probability(new_stage)
                await conn.execute(text("""
                    INSERT INTO deals (company_id, name, value, stage, probability, assigned_to, created_at)
                    VALUES (:c, :n, :v, :s, :p, :a, :ts)
                """), {"c": lid, "n": f"{bname} — Initial Deal", "v": value,
                       "s": new_stage, "p": prob, "a": owner, "ts": created})
            if deal_leads:
                print(f"Step 5: created {len(deal_leads)} deal(s) from leads with deal_value/deal_stage")

        # ----- Step 6: activities/tasks lead_id -> company_id -----
        for tbl in ("activities", "tasks"):
            cols = await _columns(conn, tbl)
            if "lead_id" in cols and "company_id" in cols:
                await conn.execute(text(f"""
                    UPDATE {tbl}
                    SET company_id = lead_id
                    WHERE company_id IS NULL AND lead_id IS NOT NULL
                """))
                print(f"Step 6: backfilled {tbl}.company_id from lead_id")

        # ----- Step 7: generated_emails lead_id -> contact_id + company_id -----
        ge_cols = await _columns(conn, "generated_emails")
        if "lead_id" in ge_cols and "contact_id" in ge_cols and "company_id" in ge_cols:
            # contact_id: primary contact for the company that matches lead_id
            await conn.execute(text("""
                UPDATE generated_emails
                SET contact_id = (
                    SELECT id FROM contacts
                    WHERE company_id = generated_emails.lead_id
                    ORDER BY is_primary DESC, id ASC
                    LIMIT 1
                )
                WHERE contact_id IS NULL AND lead_id IS NOT NULL
            """))
            await conn.execute(text("""
                UPDATE generated_emails
                SET company_id = lead_id
                WHERE company_id IS NULL AND lead_id IS NOT NULL
            """))
            print("Step 7: backfilled generated_emails.contact_id + company_id from lead_id")

        # ----- Step 8: lead_tags -> company_tags -----
        if await _table_exists(conn, "lead_tags"):
            await conn.execute(text("""
                INSERT OR IGNORE INTO company_tags (company_id, tag_id)
                SELECT lead_id, tag_id FROM lead_tags
            """))
            print("Step 8: copied lead_tags -> company_tags")

        # ----- Step 9: rebuild activities/tasks/generated_emails without lead_id ----
        # SQLite can't DROP COLUMN when an FK definition references the column,
        # so we recreate the table without it. This is the "table swap" pattern.
        await _rebuild_activities(conn)
        await _rebuild_tasks(conn)
        await _rebuild_generated_emails(conn)

        # ----- Step 10: drop old tables -----
        for old_table in ("lead_tags", "leads"):
            if await _table_exists(conn, old_table):
                await conn.execute(text(f"DROP TABLE {old_table}"))
                print(f"Step 10: - dropped table {old_table}")

    print("Migration complete.")


async def _rebuild_activities(conn) -> None:
    cols = await _columns(conn, "activities")
    if "lead_id" not in cols:
        return  # already rebuilt
    await conn.execute(text("""
        CREATE TABLE activities_new (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id      INTEGER NOT NULL REFERENCES companies(id),
            contact_id      INTEGER REFERENCES contacts(id),
            deal_id         INTEGER REFERENCES deals(id),
            user_id         INTEGER REFERENCES users(id),
            activity_type   VARCHAR(50) NOT NULL,
            content         TEXT DEFAULT '',
            metadata_json   TEXT,
            created_at      DATETIME DEFAULT (datetime('now'))
        )
    """))
    await conn.execute(text("""
        INSERT INTO activities_new (id, company_id, contact_id, deal_id, user_id, activity_type, content, metadata_json, created_at)
        SELECT id, company_id, contact_id, deal_id, user_id, activity_type, content, metadata_json, created_at
        FROM activities
        WHERE company_id IS NOT NULL
    """))
    await conn.execute(text("DROP TABLE activities"))
    await conn.execute(text("ALTER TABLE activities_new RENAME TO activities"))
    print("Step 9: rebuilt activities (dropped lead_id)")


async def _rebuild_tasks(conn) -> None:
    cols = await _columns(conn, "tasks")
    if "lead_id" not in cols:
        return
    await conn.execute(text("""
        CREATE TABLE tasks_new (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id      INTEGER NOT NULL REFERENCES companies(id),
            contact_id      INTEGER REFERENCES contacts(id),
            deal_id         INTEGER REFERENCES deals(id),
            user_id         INTEGER NOT NULL REFERENCES users(id),
            description     VARCHAR(500) NOT NULL,
            due_date        DATETIME,
            completed       BOOLEAN DEFAULT 0,
            completed_at    DATETIME,
            created_at      DATETIME DEFAULT (datetime('now'))
        )
    """))
    await conn.execute(text("""
        INSERT INTO tasks_new (id, company_id, contact_id, deal_id, user_id, description, due_date, completed, completed_at, created_at)
        SELECT id, company_id, contact_id, deal_id, user_id, description, due_date, completed, completed_at, created_at
        FROM tasks
        WHERE company_id IS NOT NULL
    """))
    await conn.execute(text("DROP TABLE tasks"))
    await conn.execute(text("ALTER TABLE tasks_new RENAME TO tasks"))
    print("Step 9: rebuilt tasks (dropped lead_id)")


async def _rebuild_generated_emails(conn) -> None:
    cols = await _columns(conn, "generated_emails")
    if "lead_id" not in cols:
        return
    await conn.execute(text("""
        CREATE TABLE generated_emails_new (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id      INTEGER NOT NULL REFERENCES contacts(id),
            company_id      INTEGER NOT NULL REFERENCES companies(id),
            subject         VARCHAR(500) NOT NULL,
            body            TEXT NOT NULL,
            email_type      VARCHAR(50) DEFAULT 'cold',
            sequence_order  INTEGER DEFAULT 1,
            send_delay_days INTEGER DEFAULT 0,
            scheduled_send_at DATETIME,
            sent_at         DATETIME,
            is_sent         BOOLEAN DEFAULT 0,
            paused_at       DATETIME,
            problems_referenced TEXT,
            created_at      DATETIME DEFAULT (datetime('now'))
        )
    """))
    await conn.execute(text("""
        INSERT INTO generated_emails_new (
            id, contact_id, company_id, subject, body, email_type, sequence_order,
            send_delay_days, scheduled_send_at, sent_at, is_sent, paused_at,
            problems_referenced, created_at
        )
        SELECT
            id, contact_id, company_id, subject, body, email_type, sequence_order,
            send_delay_days, scheduled_send_at, sent_at, is_sent, paused_at,
            problems_referenced, created_at
        FROM generated_emails
        WHERE contact_id IS NOT NULL AND company_id IS NOT NULL
    """))
    await conn.execute(text("DROP TABLE generated_emails"))
    await conn.execute(text("ALTER TABLE generated_emails_new RENAME TO generated_emails"))
    print("Step 9: rebuilt generated_emails (dropped lead_id)")


def _split_name(full: str | None) -> tuple[str, str]:
    if not full:
        return "", ""
    parts = full.strip().split(maxsplit=1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _stage_probability(stage: str) -> int:
    return {
        "prospecting": 5,
        "qualified":   15,
        "proposal":    35,
        "negotiation": 65,
        "closed_won":  100,
        "closed_lost": 0,
    }.get(stage, 5)


if __name__ == "__main__":
    asyncio.run(main())
