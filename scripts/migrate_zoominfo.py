"""
Add ZoomInfo PKI credentials + cached access token columns to runtime_config.

ZoomInfo doesn't use a simple API key. Instead:
  1. Tenant registers an app in their ZoomInfo developer portal
  2. Gets username (account email) + client_id + RSA private key
  3. We sign a short-lived JWT with the private key, exchange it at
     /authenticate for an access token (24h validity), cache + reuse

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


COLUMNS = [
    ("zoominfo_username",          "TEXT"),
    ("zoominfo_client_id",         "TEXT"),
    ("zoominfo_private_key",       "TEXT"),
    ("zoominfo_access_token",      "TEXT"),
    ("zoominfo_token_expires_at",  "DATETIME"),
]


async def main() -> None:
    async with engine.begin() as conn:
        for name, ddl in COLUMNS:
            if not await column_exists(conn, "runtime_config", name):
                await conn.execute(text(f"ALTER TABLE runtime_config ADD COLUMN {name} {ddl}"))
                print(f"+ added runtime_config.{name}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
