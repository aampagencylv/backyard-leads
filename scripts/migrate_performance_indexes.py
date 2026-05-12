"""Add performance indexes for Postgres — eliminates full table scans on
common WHERE/JOIN columns. Each index is IF NOT EXISTS so re-running is safe."""
import asyncio
from sqlalchemy import text
from app.database import engine

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_company_status ON companies(status)",
    "CREATE INDEX IF NOT EXISTS idx_company_domain ON companies(domain)",
    "CREATE INDEX IF NOT EXISTS idx_company_assigned_to ON companies(assigned_to)",
    "CREATE INDEX IF NOT EXISTS idx_deal_stage ON deals(stage)",
    "CREATE INDEX IF NOT EXISTS idx_deal_company_id ON deals(company_id)",
    "CREATE INDEX IF NOT EXISTS idx_deal_assigned_to ON deals(assigned_to)",
    "CREATE INDEX IF NOT EXISTS idx_activity_company_id ON activities(company_id)",
    "CREATE INDEX IF NOT EXISTS idx_activity_contact_id ON activities(contact_id)",
    "CREATE INDEX IF NOT EXISTS idx_activity_type ON activities(activity_type)",
    "CREATE INDEX IF NOT EXISTS idx_activity_created_at ON activities(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_activity_user_id ON activities(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_contact_company_id ON contacts(company_id)",
    "CREATE INDEX IF NOT EXISTS idx_contact_email ON contacts(email)",
    "CREATE INDEX IF NOT EXISTS idx_ge_contact_id ON generated_emails(contact_id)",
    "CREATE INDEX IF NOT EXISTS idx_ge_company_id ON generated_emails(company_id)",
    "CREATE INDEX IF NOT EXISTS idx_ge_is_sent ON generated_emails(is_sent)",
    "CREATE INDEX IF NOT EXISTS idx_ge_scheduled ON generated_emails(scheduled_send_at)",
    "CREATE INDEX IF NOT EXISTS idx_ge_label ON generated_emails(sequence_label)",
    "CREATE INDEX IF NOT EXISTS idx_task_user_id ON tasks(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_company_id ON tasks(company_id)",
    "CREATE INDEX IF NOT EXISTS idx_booking_host ON bookings(host_user_id)",
    "CREATE INDEX IF NOT EXISTS idx_booking_starts ON bookings(starts_at)",
]

async def main():
    async with engine.begin() as conn:
        created = 0
        for idx_sql in INDEXES:
            try:
                await conn.execute(text(idx_sql))
                created += 1
            except Exception as e:
                print(f"  skip: {e}")
    print(f"[performance_indexes] {created}/{len(INDEXES)} indexes ensured")

if __name__ == "__main__":
    asyncio.run(main())
