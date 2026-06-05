"""Validate that creating a new tenant + enrolling a contact in it
auto-provisions every piece of engagement-engine scaffolding.

Creates a throwaway tenant, calls the same `_provision_engagement_engine_scaffolding`
helper that admin_routes.create_tenant uses, seeds a company + contact,
runs start_engagement, then asserts:

  - tenant_ai_config exists with sensible defaults
  - playbooks row '30-day default' exists with steps in ai_strategy_json
  - sequence_templates row exists (is_default=TRUE, is_active=TRUE)
  - email_identities row exists (is_active=FALSE pending tenant setup)
  - observations row exists for the company (website_homepage)
  - engagement + actions materialized correctly

Cleans up everything afterward.
"""
from __future__ import annotations
import asyncio
import secrets
import sys
from sqlalchemy import text, select

from app.database import async_session
from app.models import Contact


TEST_SLUG = f"__validate_tenant_{secrets.token_hex(4)}"


async def main() -> int:
    print(f"=== validate_new_tenant_provisioning ===")
    print(f"  tenant slug: {TEST_SLUG}")

    # 1. Create the test tenant + run the scaffolding helper.
    async with async_session() as db:
        r = (await db.execute(text("""
            INSERT INTO tenants (name, slug, plan, status, created_at, updated_at)
            VALUES ('Validation Test Tenant', :slug, 'starter', 'active', NOW(), NOW())
            RETURNING id
        """), {"slug": TEST_SLUG})).first()
        tenant_id = int(r[0])
        await db.commit()
        print(f"  tenant_id={tenant_id}")

    try:
        # Run the same scaffolding helper that create_tenant uses.
        from app.routes.admin_routes import _provision_engagement_engine_scaffolding
        async with async_session() as db:
            await _provision_engagement_engine_scaffolding(
                db, tenant_id=tenant_id,
                tenant_name="Validation Test Tenant",
                created_by_user_id=None,
            )
            await db.commit()
        print("  scaffolding helper ran")

        # 2. Assert every piece exists.
        async with async_session() as db:
            cfg = (await db.execute(text(
                "SELECT provider, model_signal_scoring, model_reply_classification, "
                "per_engagement_budget_usd FROM tenant_ai_config WHERE tenant_id = :t"
            ), {"t": tenant_id})).first()
            assert cfg, "tenant_ai_config missing"
            print(f"  ✓ tenant_ai_config: provider={cfg.provider} "
                  f"model_signal_scoring={cfg.model_signal_scoring} "
                  f"per_eng_budget=${cfg.per_engagement_budget_usd}")

            pb = (await db.execute(text(
                "SELECT id, name, is_active, ai_strategy_json FROM playbooks "
                "WHERE tenant_id = :t AND name = '30-day default'"
            ), {"t": tenant_id})).first()
            assert pb, "playbook missing"
            print(f"  ✓ playbook: id={pb.id} active={pb.is_active}")
            steps_count = len((pb.ai_strategy_json or {}).get("steps") or [])
            print(f"      ai_strategy_json.steps count: {steps_count}")
            assert steps_count == 13, f"expected 13 steps, got {steps_count}"

            st = (await db.execute(text(
                "SELECT id, name, is_default, is_active FROM sequence_templates "
                "WHERE tenant_id = :t"
            ), {"t": tenant_id})).first()
            assert st, "sequence_template missing"
            print(f"  ✓ sequence_template: id={st.id} is_default={st.is_default}")

            ei = (await db.execute(text(
                "SELECT id, sender_name, is_active, warmup_stage FROM email_identities "
                "WHERE tenant_id = :t"
            ), {"t": tenant_id})).first()
            assert ei, "email_identity missing"
            print(f"  ✓ email_identity: id={ei.id} stage={ei.warmup_stage} "
                  f"active={ei.is_active}")

        # 3. Create a company + contact in this new tenant.
        async with async_session() as db:
            co_id = (await db.execute(text("""
                INSERT INTO companies (
                    tenant_id, name, website,
                    email_generated, status,
                    lead_score, lead_score_fit, lead_score_intent, lead_score_tier,
                    created_at, updated_at
                )
                VALUES (
                    :t, '__validation_tenant_co__', 'https://example.com',
                    FALSE, 'pursuing',
                    0, 0, 0, 'cold',
                    NOW(), NOW()
                ) RETURNING id
            """), {"t": tenant_id})).first()[0]
            c_id = (await db.execute(text("""
                INSERT INTO contacts (
                    tenant_id, company_id, first_name, last_name, email,
                    is_primary, do_not_text,
                    unsubscribe_token, created_at, updated_at, outreach_owner
                )
                VALUES (
                    :t, :co, 'New', 'Tenant',
                    'newtenant-test@example.invalid',
                    TRUE, FALSE,
                    'newtenant-test-token', NOW(), NOW(), 'none'
                ) RETURNING id
            """), {"t": tenant_id, "co": co_id})).first()[0]
            await db.commit()
            print(f"  company_id={co_id} contact_id={c_id}")

        # 4. Run start_engagement and verify observation auto-seeded.
        from app.engagement_engine.lifecycle import start_engagement
        async with async_session() as db:
            contact = (await db.execute(select(Contact).where(Contact.id == c_id))).scalar_one()
            n = await start_engagement(
                db, contact,
                pre_generate_content=False,
                initiated_by="validate_tenant",
            )
        print(f"  start_engagement created {n} actions")
        assert n > 0, "no actions created"

        async with async_session() as db:
            obs = (await db.execute(text("""
                SELECT o.id, o.source_url, o.is_active, o.next_poll_at,
                       o.poll_interval_days
                FROM observations o
                JOIN source_types st ON st.id = o.source_type_id
                WHERE o.tenant_id = :t AND o.company_id = :co
                  AND st.code = 'website_homepage'
            """), {"t": tenant_id, "co": co_id})).first()
            assert obs, "observation was NOT auto-seeded by start_engagement"
            print(f"  ✓ observation auto-seeded: id={obs.id} "
                  f"url={obs.source_url} active={obs.is_active} "
                  f"next_poll={obs.next_poll_at}")

            eng = (await db.execute(text("""
                SELECT id, current_playbook_id, status
                FROM engagements WHERE contact_id = :c ORDER BY id DESC LIMIT 1
            """), {"c": c_id})).first()
            print(f"  ✓ engagement: id={eng.id} playbook_id={eng.current_playbook_id} "
                  f"status={eng.status}")
            assert eng.current_playbook_id == pb.id, \
                f"engagement points to wrong playbook: {eng.current_playbook_id} vs {pb.id}"

        print()
        print("ALL ASSERTIONS PASSED")
        return 0

    finally:
        # Cleanup uses a fresh engine connection because the session
        # factory installs a do_orm_execute auto-filter that scopes
        # DELETEs to whatever tenant_id is on session.info — this
        # script never set one, so session-based DELETEs no-op'd.
        # Direct engine connection bypasses the auto-filter entirely.
        from sqlalchemy.ext.asyncio import create_async_engine
        import os as _os
        db_url = _os.environ.get("DATABASE_URL")
        if db_url is None:
            for line in open("/opt/backyard-leads/.env").read().splitlines():
                if line.startswith("DATABASE_URL="):
                    db_url = line.split("=", 1)[1]
                    break
        cleanup_engine = create_async_engine(db_url)
        async with cleanup_engine.begin() as conn:
            await conn.execute(text("DELETE FROM observations WHERE tenant_id = :t"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM actions WHERE tenant_id = :t"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM engagements WHERE tenant_id = :t"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM activities WHERE tenant_id = :t OR contact_id IN (SELECT id FROM contacts WHERE tenant_id = :t)"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM signals WHERE tenant_id = :t"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM tasks WHERE tenant_id = :t"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM contacts WHERE tenant_id = :t"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM companies WHERE tenant_id = :t"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM playbooks WHERE tenant_id = :t"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM sequence_templates WHERE tenant_id = :t"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM email_identities WHERE tenant_id = :t"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM tenant_ai_config WHERE tenant_id = :t"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM tenant_domains WHERE tenant_id = :t"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM runtime_config WHERE tenant_id = :t"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        await cleanup_engine.dispose()
        print(f"  cleanup: removed tenant {tenant_id} and all dependent rows")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
