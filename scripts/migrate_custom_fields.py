"""
Add custom_field_definitions table + custom_fields_json columns on
companies and contacts. Pre-seeds BMP defaults: Facebook page, Instagram
page, annual revenue (company) and Instagram handle (contact).

Tenants add their own fields from Settings → Custom Fields.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


CREATE_DEFS = """
CREATE TABLE IF NOT EXISTS custom_field_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type VARCHAR(20) NOT NULL,
    key VARCHAR(80) NOT NULL,
    label VARCHAR(120) NOT NULL,
    field_type VARCHAR(20) NOT NULL DEFAULT 'text',
    options_json TEXT,
    helper_text VARCHAR(200),
    display_order INTEGER NOT NULL DEFAULT 100,
    is_active BOOLEAN NOT NULL DEFAULT 1,
    is_default BOOLEAN NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_custom_field_definitions_entity_type ON custom_field_definitions(entity_type)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_custom_field_definitions_entity_key ON custom_field_definitions(entity_type, key)",
]

# (entity_type, key, label, field_type, helper_text, display_order)
DEFAULTS = [
    ("company", "facebook_page",  "Facebook Page",  "url",     "Full URL of the company's Facebook page",       10),
    ("company", "instagram_page", "Instagram Page", "url",     "Full URL of the company's Instagram profile",   11),
    ("company", "twitter_handle", "X / Twitter",    "text",    "@-handle without the @, or full URL",           12),
    ("company", "annual_revenue", "Annual Revenue", "number",  "USD; rough estimate is fine",                   20),
    ("company", "year_founded",   "Year Founded",   "number",  "4-digit year; helps signal stability",          21),
    ("contact", "instagram_handle","Instagram",     "text",    "@-handle without the @",                        10),
    ("contact", "twitter_handle", "X / Twitter",    "text",    "@-handle without the @",                        11),
    ("contact", "personal_email", "Personal Email", "email",   "Backup email if their work address bounces",    20),
]


async def main() -> None:
    async with engine.begin() as conn:
        defs_existed = (await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='custom_field_definitions'")
        )).scalar_one_or_none()
        await conn.execute(text(CREATE_DEFS))
        if not defs_existed:
            print("+ created custom_field_definitions table")
        for idx_sql in INDEXES:
            await conn.execute(text(idx_sql))

        # custom_fields_json columns on companies + contacts
        for table in ("companies", "contacts"):
            cols = {r[1] for r in (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()}
            if not await column_exists(conn, table, "custom_fields_json"):
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN custom_fields_json TEXT"))
                print(f"+ added {table}.custom_fields_json")

        # Seed BMP defaults — INSERT OR IGNORE against the unique index so
        # re-running the migration doesn't duplicate or stomp tenant edits.
        from datetime import datetime, timezone as _tz
        now = datetime.now(_tz.utc).isoformat()
        seeded = 0
        for entity, key, label, ftype, helper, order in DEFAULTS:
            res = await conn.execute(
                text(
                    "INSERT OR IGNORE INTO custom_field_definitions "
                    "(entity_type, key, label, field_type, helper_text, display_order, "
                    " is_active, is_default, created_at, updated_at) "
                    "VALUES (:e, :k, :lbl, :ft, :h, :o, 1, 1, :ts, :ts)"
                ),
                {"e": entity, "k": key, "lbl": label, "ft": ftype,
                 "h": helper, "o": order, "ts": now},
            )
            if res.rowcount:
                seeded += 1
        if seeded:
            print(f"+ seeded {seeded} default custom field definitions")

        # Safety net: re-activate any default fields that got accidentally
        # deactivated (e.g. by a UI bug or bulk action). Default fields
        # should always be active unless explicitly archived by an admin.
        reactivated = (await conn.execute(
            text("UPDATE custom_field_definitions SET is_active = 1 WHERE is_default = 1 AND is_active = 0")
        )).rowcount
        if reactivated:
            print(f"+ re-activated {reactivated} default fields that were accidentally deactivated")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
