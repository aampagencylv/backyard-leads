"""
Create campaigns and campaign_logs tables for Auto Pilot.
Idempotent — safe to run multiple times.
"""
import asyncio
from sqlalchemy import text
from app.database import engine


async def migrate():
    async with engine.begin() as conn:
        tables = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='campaigns'"))
        if not tables.fetchone():
            await conn.execute(text("""
                CREATE TABLE campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(255) NOT NULL,
                    created_by INTEGER NOT NULL REFERENCES users(id),
                    business_types TEXT NOT NULL,
                    locations TEXT NOT NULL,
                    min_reviews INTEGER DEFAULT 20,
                    max_reviews INTEGER DEFAULT 300,
                    min_rating REAL DEFAULT 3.5,
                    must_have_website BOOLEAN DEFAULT 1,
                    max_ai_visibility_score INTEGER DEFAULT 40,
                    min_problems INTEGER DEFAULT 3,
                    contact_required BOOLEAN DEFAULT 1,
                    max_prospects_per_day INTEGER DEFAULT 10,
                    mode VARCHAR(20) DEFAULT 'moderate',
                    contact_cooldown_days INTEGER DEFAULT 90,
                    last_assigned_index INTEGER DEFAULT 0,
                    status VARCHAR(20) DEFAULT 'draft',
                    total_locations_searched INTEGER DEFAULT 0,
                    total_prospects_found INTEGER DEFAULT 0,
                    total_qualified INTEGER DEFAULT 0,
                    total_sequences_created INTEGER DEFAULT 0,
                    total_emails_sent INTEGER DEFAULT 0,
                    total_replies INTEGER DEFAULT 0,
                    current_location_index INTEGER DEFAULT 0,
                    current_business_type_index INTEGER DEFAULT 0,
                    prospects_today INTEGER DEFAULT 0,
                    last_run_at DATETIME,
                    last_daily_reset DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            print("migrate_campaigns: created campaigns table")

        tables = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='campaign_members'"))
        if not tables.fetchone():
            await conn.execute(text("""
                CREATE TABLE campaign_members (
                    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    PRIMARY KEY (campaign_id, user_id)
                )
            """))
            print("migrate_campaigns: created campaign_members table")

        tables = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='campaign_logs'"))
        if not tables.fetchone():
            await conn.execute(text("""
                CREATE TABLE campaign_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
                    action VARCHAR(50) NOT NULL,
                    detail TEXT DEFAULT '',
                    company_id INTEGER,
                    contact_id INTEGER,
                    created_at DATETIME
                )
            """))
            print("migrate_campaigns: created campaign_logs table")

        # Add step_type to generated_emails
        ge_cols = await conn.execute(text("PRAGMA table_info(generated_emails)"))
        ge_col_names = [r[1] for r in ge_cols.fetchall()]
        if "step_type" not in ge_col_names:
            await conn.execute(text("ALTER TABLE generated_emails ADD COLUMN step_type VARCHAR(20) DEFAULT 'email'"))
            print("migrate_campaigns: added generated_emails.step_type")

        # Add package fields to deals
        deal_cols = await conn.execute(text("PRAGMA table_info(deals)"))
        deal_col_names = [r[1] for r in deal_cols.fetchall()]

        if "package" not in deal_col_names:
            await conn.execute(text("ALTER TABLE deals ADD COLUMN package VARCHAR(50)"))
            print("migrate_campaigns: added deals.package")

        if "contract_months" not in deal_col_names:
            await conn.execute(text("ALTER TABLE deals ADD COLUMN contract_months INTEGER DEFAULT 6"))
            print("migrate_campaigns: added deals.contract_months")


if __name__ == "__main__":
    asyncio.run(migrate())
