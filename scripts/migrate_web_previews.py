"""Web preview storage.

Per-prospect generated single-page website previews. Lives behind a
short token URL the rep pastes into a cold email.

Schema:
  - id              SERIAL PK
  - tenant_id       FK tenants (the agency that owns this preview)
  - company_id      FK companies (the prospect)
  - created_by      FK users (the rep)
  - template_slug   which design.md template was used
  - url_slug        '{company-slug}-{token}' for routing
  - html            the rendered HTML (so we don't re-render on each view)
  - slots_json      the LLM's slot output (kept so we can edit later
                    without re-generating the whole thing)
  - photos_json     {hero, about, gallery[]} actually used
  - cta_url         where the CTA points (per-preview override possible)
  - view_count      incremented on each GET /sitepreview/{slug}
  - first_viewed_at when the prospect first opened it
  - last_viewed_at  most recent view
  - cta_click_count CTA click counter (tracked via small inline JS)
  - cost_usd        what this preview cost us to generate
  - status          active | archived
  - created_at
  - expires_at      30-day default, NULL = never

Indexed on (url_slug) unique for the public-facing route + (tenant_id,
created_at) for the rep's "my previews" view.

Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine
from app.services.migration_utils import table_exists


async def main() -> None:
    async with engine.begin() as conn:
        if not await table_exists(conn, "web_previews"):
            await conn.execute(text("""
                CREATE TABLE web_previews (
                    id              SERIAL PRIMARY KEY,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(id),
                    company_id      INTEGER NOT NULL REFERENCES companies(id),
                    created_by      INTEGER REFERENCES users(id),
                    template_slug   VARCHAR(64) NOT NULL,
                    url_slug        VARCHAR(128) NOT NULL UNIQUE,
                    html            TEXT NOT NULL,
                    slots_json      TEXT NOT NULL,
                    photos_json     TEXT,
                    cta_url         VARCHAR(500),
                    view_count      INTEGER NOT NULL DEFAULT 0,
                    first_viewed_at TIMESTAMPTZ,
                    last_viewed_at  TIMESTAMPTZ,
                    cta_click_count INTEGER NOT NULL DEFAULT 0,
                    cost_usd        FLOAT NOT NULL DEFAULT 0,
                    status          VARCHAR(20) NOT NULL DEFAULT 'active',
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at      TIMESTAMPTZ
                )
            """))
            await conn.execute(text(
                "CREATE INDEX ix_web_previews_tenant_company "
                "ON web_previews(tenant_id, company_id)"
            ))
            await conn.execute(text(
                "CREATE INDEX ix_web_previews_created_at "
                "ON web_previews(created_at DESC)"
            ))
            print("+ web_previews table created")
    print("Migration complete — web_previews ready.")


if __name__ == "__main__":
    asyncio.run(main())
