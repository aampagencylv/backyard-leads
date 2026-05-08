"""
Add api_keys + webhooks tables for the public API surface.

  api_keys  — personal API keys; SHA-256-hashed at rest, plaintext shown
              once at creation. Format: pk_live_<hex64>
  webhooks  — outbound event subscriptions with HMAC-SHA256 signing.
              JSON list of events; empty = all events.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


CREATE_API_KEYS = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name VARCHAR(80) NOT NULL,
    key_hash VARCHAR(64) NOT NULL UNIQUE,
    key_prefix VARCHAR(20) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT 1,
    last_used_at DATETIME,
    created_at DATETIME NOT NULL
)
"""

CREATE_WEBHOOKS = """
CREATE TABLE IF NOT EXISTS webhooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name VARCHAR(80) NOT NULL,
    url VARCHAR(500) NOT NULL,
    secret VARCHAR(80) NOT NULL,
    events_json TEXT,
    is_active BOOLEAN NOT NULL DEFAULT 1,
    last_delivery_at DATETIME,
    last_delivery_status INTEGER,
    last_delivery_error VARCHAR(300),
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_api_keys_user_id ON api_keys(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_api_keys_key_hash ON api_keys(key_hash)",
    "CREATE INDEX IF NOT EXISTS ix_webhooks_user_id ON webhooks(user_id)",
]


async def main() -> None:
    async with engine.begin() as conn:
        for name, sql in (("api_keys", CREATE_API_KEYS), ("webhooks", CREATE_WEBHOOKS)):
            existed = (await conn.execute(
                text(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{name}'")
            )).scalar_one_or_none()
            await conn.execute(text(sql))
            if not existed:
                print(f"+ created {name} table")
        for idx in INDEXES:
            await conn.execute(text(idx))
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
