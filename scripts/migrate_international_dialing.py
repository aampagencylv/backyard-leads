"""Add international dialing configuration.

New tables:
- country_dialing_config: reference table with compliance rules per country

New columns:
- tenant_ai_config.supported_calling_countries_json: which countries this tenant can call
- contacts.phone_country_code: ISO country code inferred from phone number
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def _col_exists(conn, table, col):
    r = await conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=:t AND column_name=:c)"
    ), {"t": table, "c": col})
    return bool(r.scalar())


async def _table_exists(conn, table):
    r = await conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=:t)"
    ), {"t": table})
    return bool(r.scalar())


async def main():
    async with engine.begin() as conn:
        print("Checking for country_dialing_config table...")
        if not await _table_exists(conn, "country_dialing_config"):
            print("+ Creating country_dialing_config table")
            await conn.execute(text("""
                CREATE TABLE country_dialing_config (
                    id SERIAL PRIMARY KEY,
                    iso_country VARCHAR(2) NOT NULL UNIQUE,
                    country_name VARCHAR(100) NOT NULL,
                    calling_window_start INTEGER NOT NULL DEFAULT 8,
                    calling_window_end INTEGER NOT NULL DEFAULT 21,
                    default_timezone VARCHAR(50) NOT NULL DEFAULT 'America/New_York',
                    caller_id_verification_required BOOLEAN NOT NULL DEFAULT FALSE,
                    recording_consent_required BOOLEAN NOT NULL DEFAULT FALSE,
                    requires_local_number_registration BOOLEAN NOT NULL DEFAULT FALSE,
                    compliance_notes TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            print("  seeding with US, MX, BR defaults...")
            await conn.execute(text("""
                INSERT INTO country_dialing_config
                  (iso_country, country_name, calling_window_start, calling_window_end,
                   default_timezone, caller_id_verification_required,
                   recording_consent_required, requires_local_number_registration,
                   compliance_notes)
                VALUES
                  ('US', 'United States', 8, 21, 'America/New_York', FALSE, FALSE, FALSE, 'TCPA 8am-9pm local time'),
                  ('MX', 'Mexico', 8, 21, 'America/Mexico_City', TRUE, TRUE, TRUE,
                   'COFETEL registration + caller ID verification required; recording consent mandatory'),
                  ('BR', 'Brazil', 8, 21, 'America/Sao_Paulo', TRUE, TRUE, FALSE,
                   'Caller ID verification required; recording consent mandatory'),
                  ('CO', 'Colombia', 8, 21, 'America/Bogota', FALSE, TRUE, FALSE,
                   'Recording consent required'),
                  ('CL', 'Chile', 8, 21, 'America/Santiago', FALSE, TRUE, FALSE,
                   'Recording consent required'),
                  ('AR', 'Argentina', 8, 21, 'America/Argentina/Buenos_Aires', FALSE, TRUE, FALSE,
                   'Recording consent required')
            """))
        else:
            print("= country_dialing_config already exists")

        print("\nChecking tenant_ai_config.supported_calling_countries_json...")
        if not await _col_exists(conn, "tenant_ai_config", "supported_calling_countries_json"):
            print("+ Adding supported_calling_countries_json column")
            await conn.execute(text("""
                ALTER TABLE tenant_ai_config
                ADD COLUMN supported_calling_countries_json TEXT DEFAULT '[]'
            """))
        else:
            print("= column already exists")

        print("\nChecking contacts.phone_country_code...")
        if not await _col_exists(conn, "contacts", "phone_country_code"):
            print("+ Adding phone_country_code column")
            await conn.execute(text("""
                ALTER TABLE contacts
                ADD COLUMN phone_country_code VARCHAR(2)
            """))
        else:
            print("= column already exists")

        print("\n✓ Migration complete")


if __name__ == "__main__":
    asyncio.run(main())
