"""
Add audit_log table — append-only record of privileged actions.

Drives Settings → Audit Log (admin-visible) and is required for
SOC2 / enterprise sales conversations. Indexed for the three most
common admin queries: by date, by actor, by action verb.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER REFERENCES users(id),
    actor_email VARCHAR(255),
    actor_role VARCHAR(20),
    action VARCHAR(80) NOT NULL,
    target_type VARCHAR(40),
    target_id INTEGER,
    target_label VARCHAR(255),
    metadata_json TEXT,
    ip_address VARCHAR(64),
    user_agent VARCHAR(300),
    created_at DATETIME NOT NULL
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_audit_log_actor_user_id ON audit_log(actor_user_id)",
    "CREATE INDEX IF NOT EXISTS ix_audit_log_action ON audit_log(action)",
    "CREATE INDEX IF NOT EXISTS ix_audit_log_target_type ON audit_log(target_type)",
    "CREATE INDEX IF NOT EXISTS ix_audit_log_created_at ON audit_log(created_at)",
]


async def main() -> None:
    async with engine.begin() as conn:
        existed = (await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'")
        )).scalar_one_or_none()
        await conn.execute(text(CREATE_SQL))
        if not existed:
            print("+ created audit_log table")
        for idx in INDEXES:
            await conn.execute(text(idx))
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
