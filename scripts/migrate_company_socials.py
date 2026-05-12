"""
Promote social profile URLs to first-class Company columns + retire the
'these are auto-derivable' custom-field defaults that were seeded by
mistake (facebook_page, instagram_page, twitter_handle, annual_revenue,
year_founded). Steve flagged: those should be auto-populated by
scraping / ZoomInfo, not BDR data-entry. The custom fields system stays
for genuinely vertical-specific tenant data.

What changes:
  + companies.facebook_url, instagram_url, youtube_url, tiktok_url
    (auto-populated by website_intel during enrichment)
  - custom_field_definitions seeded defaults marked is_active=false
    (NOT deleted — preserves any tenant-entered values on existing
    companies / contacts)

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.services.migration_utils import column_exists
from app.database import engine


SOCIAL_COLUMNS = [
    "facebook_url",
    "instagram_url",
    "youtube_url",
    "tiktok_url",
]

# Defaults to retire — these are auto-derivable, not real custom fields.
RETIRE_DEFAULTS = [
    ("company", "facebook_page"),
    ("company", "instagram_page"),
    ("company", "twitter_handle"),
    ("company", "annual_revenue"),
    ("company", "year_founded"),
    ("contact", "instagram_handle"),
    ("contact", "twitter_handle"),
    ("contact", "personal_email"),
]


async def main() -> None:
    async with engine.begin() as conn:
        # Add social URL columns to companies if missing
        for col in SOCIAL_COLUMNS:
            if col not in cols:
                await conn.execute(text(f"ALTER TABLE companies ADD COLUMN {col} VARCHAR(500)"))
                print(f"+ added companies.{col}")

        # Retire seeded custom-field defaults — keep the rows so any values
        # users typed in stay queryable, but mark them is_active=false so
        # they vanish from the UI. Only touch rows that were seeded as
        # is_default=true (don't kill anything tenants created themselves).
        for entity, key in RETIRE_DEFAULTS:
            res = await conn.execute(
                text(
                    "UPDATE custom_field_definitions "
                    "SET is_active = 0 "
                    "WHERE entity_type = :e AND key = :k AND is_default = 1 AND is_active = 1"
                ),
                {"e": entity, "k": key},
            )
            if res.rowcount:
                print(f"- deactivated default {entity}.{key}")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
