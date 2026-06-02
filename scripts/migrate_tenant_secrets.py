"""Phase A: tenant_secrets table for encrypted per-tenant credentials.

Stores per-tenant API keys / sub-account credentials (Twilio account SID,
Resend domain creds, etc.) — encrypted at rest with AES-GCM via Fernet.

The encryption key is derived from SECRET_KEY for now (single deploy,
shared with JWT signing). When we add a separate TENANT_SECRETS_KEY
env var, the helper in app.secrets_vault will switch to it transparently.

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def _table_exists(conn, name: str) -> bool:
    r = await conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=:n)"
    ), {"n": name})
    return bool(r.scalar())


async def main() -> None:
    async with engine.begin() as conn:
        if not await _table_exists(conn, "tenant_secrets"):
            await conn.execute(text("""
                CREATE TABLE tenant_secrets (
                    id              SERIAL PRIMARY KEY,
                    tenant_id       INTEGER     NOT NULL REFERENCES tenants(id),
                    name            VARCHAR(128) NOT NULL,
                    value_encrypted BYTEA       NOT NULL,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (tenant_id, name)
                )
            """))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_tenant_secrets_tenant_id "
                "ON tenant_secrets(tenant_id)"
            ))
            print("+ created table tenant_secrets")
        else:
            # If table existed (created by Base.metadata.create_all), repair
            # column defaults on timestamps in case the ORM model lacked
            # server_default at create-time.
            await conn.execute(text(
                "ALTER TABLE tenant_secrets ALTER COLUMN created_at SET DEFAULT NOW()"
            ))
            await conn.execute(text(
                "ALTER TABLE tenant_secrets ALTER COLUMN updated_at SET DEFAULT NOW()"
            ))

    print("Migration complete — tenant_secrets ready.")


if __name__ == "__main__":
    asyncio.run(main())
