"""Per-company brand asset cache for the Web Preview generator.

Stores Google Places photos, site-scraped images, the extracted logo
URL, and the dominant brand color in one JSON blob:

    {
      "google_photos": ["https://...", ...],
      "google_photos_fetched_at": "2026-06-03T17:00:00Z",
      "site_images": [{"url": "...", "alt": "..."}, ...],
      "site_logo_url": "https://...",
      "site_brand_color": "#1976d2",
      "site_extracted_at": "2026-06-03T17:00:00Z"
    }

These power the Web Preview hero/about/gallery photo selection + brand-
color override so each preview feels like the prospect's own brand, not
the template's default.

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine
from app.services.migration_utils import column_exists


async def main() -> None:
    async with engine.begin() as conn:
        if not await column_exists(conn, "companies", "brand_assets_json"):
            await conn.execute(text(
                "ALTER TABLE companies ADD COLUMN brand_assets_json TEXT NULL"
            ))
            print("+ companies.brand_assets_json added")
    print("Migration complete — brand assets cache ready.")


if __name__ == "__main__":
    asyncio.run(main())
