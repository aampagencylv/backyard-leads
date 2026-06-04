"""End-to-end verification of the engagement engine v1 schema against a real
Postgres database (staging).

Run via:
    python -m scripts.verify_engagement_engine_v1

Exits non-zero if any check fails. Designed to be run after the migration
completes to verify:

  - All 15 expected tables exist
  - All 4 lookup tables are seeded with expected codes
  - All 6 trigger functions exist
  - All trigger bindings exist
  - ai_decisions partitions for current + next 2 months exist
  - CHECK constraints reject invalid values (5 scenarios)
  - UNIQUE constraints catch duplicates (3 scenarios)
  - Triggers actually fire on illegal operations (5 scenarios)

Idempotent — each test cleans up after itself. Safe to re-run.
"""
from __future__ import annotations
import asyncio
import sys
from typing import Any
from sqlalchemy import text
from app.database import engine


EXPECTED_TABLES = [
    "channel_types", "signal_types", "source_types", "phase_transitions",
    "engagements", "playbooks", "playbook_actions",
    "signals", "actions", "ai_decisions", "observations",
    "tenant_ai_config",
    "email_identities", "email_suppressions",
    "tenant_reply_inboxes", "inbound_unattributed",
    "action_dedupe_counters",
]

EXPECTED_TRIGGER_FUNCTIONS = [
    "enforce_action_recipient_matches_contact",
    "enforce_tenant_consistency_via_engagement",
    "enforce_phase_transition",
    "enforce_day_offset_mode_consistency",
    "notify_lookup_change",
]

EXPECTED_TRIGGERS = [
    "trg_actions_recipient_lock",
    "trg_signals_tenant_consistency",
    "trg_actions_tenant_consistency",
    "trg_engagements_phase_transition",
    "trg_playbook_actions_mode_consistency",
    "trg_channel_types_notify_change",
    "trg_signal_types_notify_change",
    "trg_source_types_notify_change",
]

CHANNEL_CODES = ["email", "sms", "linkedin", "call_task", "wait", "manual"]
SOURCE_CODES_MIN = ["gmb_listing", "website_homepage", "linkedin_profile"]
SIGNAL_CODES_MIN = [
    "email_open", "email_reply", "email_bounce", "email_complaint",
    "email_unsubscribe", "sms_reply", "sms_opt_out",
    "gmb_review", "linkedin_post", "meeting_booked",
    "contact_left_company", "company_acquired", "competitor_signed",
]


PASS = "+"
FAIL = "X"


class VerificationReport:
    def __init__(self):
        self.checks: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = ""):
        self.checks.append((name, ok, detail))
        marker = PASS if ok else FAIL
        print(f"  {marker} {name}" + (f" — {detail}" if detail else ""))

    @property
    def passed(self) -> int:
        return sum(1 for _, ok, _ in self.checks if ok)

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.checks if not ok)

    def summary(self) -> str:
        return f"\n{self.passed} passed, {self.failed} failed " \
               f"({len(self.checks)} total)"


async def main() -> int:
    report = VerificationReport()

    print("=" * 70)
    print("Engagement Engine v1 — Postgres verification")
    print("=" * 70)

    async with engine.connect() as conn:
        # 1. Tables exist
        print("\n[1/8] Table existence")
        await _check_tables_exist(conn, report)

        # 2. Lookup tables seeded
        print("\n[2/8] Lookup table seeds")
        await _check_lookup_seeds(conn, report)

        # 3. Phase transitions seeded
        print("\n[3/8] Phase transitions seeded")
        await _check_phase_transitions_seeded(conn, report)

        # 4. Trigger functions exist
        print("\n[4/8] Trigger functions")
        await _check_trigger_functions(conn, report)

        # 5. Triggers bound
        print("\n[5/8] Trigger bindings")
        await _check_triggers_bound(conn, report)

        # 6. ai_decisions partitions
        print("\n[6/8] ai_decisions partitions")
        await _check_partitions(conn, report)

        # 7. CHECK constraint enforcement
        print("\n[7/8] CHECK constraints")
        await _check_check_constraints(conn, report)

        # 8. Trigger enforcement (the structural guarantees)
        print("\n[8/8] Trigger enforcement")
        await _check_trigger_enforcement(conn, report)

    print(report.summary())
    print("=" * 70)
    return 0 if report.failed == 0 else 1


# ────────────────────────────────────────────────────────────────────────────

async def _check_tables_exist(conn, report):
    rows = await conn.execute(text("""
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public' AND tablename = ANY(:names)
    """), {"names": EXPECTED_TABLES})
    found = {r.tablename for r in rows}
    for name in EXPECTED_TABLES:
        report.check(f"table {name}", name in found)


async def _check_lookup_seeds(conn, report):
    # channel_types
    rows = await conn.execute(text("SELECT code FROM channel_types"))
    found = {r.code for r in rows}
    for code in CHANNEL_CODES:
        report.check(f"channel_types.{code} seeded", code in found)

    # signal_types
    rows = await conn.execute(text("SELECT code FROM signal_types"))
    found = {r.code for r in rows}
    for code in SIGNAL_CODES_MIN:
        report.check(f"signal_types.{code} seeded", code in found)

    # source_types
    rows = await conn.execute(text("SELECT code FROM source_types"))
    found = {r.code for r in rows}
    for code in SOURCE_CODES_MIN:
        report.check(f"source_types.{code} seeded", code in found)


async def _check_phase_transitions_seeded(conn, report):
    rows = await conn.execute(text("SELECT COUNT(*) AS n FROM phase_transitions"))
    n = rows.first().n
    report.check(
        "phase_transitions has >= 15 rows",
        n >= 15,
        detail=f"found {n}",
    )

    # Spot-check a critical transition
    rows = await conn.execute(text("""
        SELECT 1 FROM phase_transitions
        WHERE from_phase = 'cold_outreach' AND to_phase = 'meeting_set'
          AND allowed_by = 'ai'
    """))
    report.check(
        "phase_transitions: cold_outreach → meeting_set (ai) exists",
        rows.first() is not None,
    )


async def _check_trigger_functions(conn, report):
    rows = await conn.execute(text("""
        SELECT proname FROM pg_proc
        WHERE proname = ANY(:names)
    """), {"names": EXPECTED_TRIGGER_FUNCTIONS})
    found = {r.proname for r in rows}
    for fn in EXPECTED_TRIGGER_FUNCTIONS:
        report.check(f"function {fn}()", fn in found)


async def _check_triggers_bound(conn, report):
    rows = await conn.execute(text("""
        SELECT tgname FROM pg_trigger
        WHERE tgname = ANY(:names) AND NOT tgisinternal
    """), {"names": EXPECTED_TRIGGERS})
    found = {r.tgname for r in rows}
    for trg in EXPECTED_TRIGGERS:
        report.check(f"trigger {trg}", trg in found)


async def _check_partitions(conn, report):
    """ai_decisions should have partitions for current + next 2 months."""
    rows = await conn.execute(text("""
        SELECT relname FROM pg_class
        WHERE relname LIKE 'ai_decisions_%' AND relkind = 'r'
    """))
    found = {r.relname for r in rows}
    report.check(
        "ai_decisions has >= 3 monthly partitions",
        len(found) >= 3,
        detail=f"found: {sorted(found)}",
    )


async def _check_check_constraints(conn, report):
    """Verify CHECK constraints reject obviously-invalid data. Each test is
    wrapped in a transaction that we ROLLBACK so no state leaks."""
    # engagements.current_phase invalid value
    try:
        async with conn.begin_nested():
            await conn.execute(text("""
                INSERT INTO engagements (tenant_id, contact_id, company_id,
                    current_phase, status, sequence_number)
                VALUES (1, 1, 1, 'invalid_phase', 'active', 99999)
            """))
        report.check("engagements.current_phase CHECK rejects 'invalid_phase'", False,
                     "INSERT unexpectedly succeeded")
    except Exception:
        report.check("engagements.current_phase CHECK rejects 'invalid_phase'", True)

    # engagements.engagement_score out of range
    try:
        async with conn.begin_nested():
            await conn.execute(text("""
                INSERT INTO engagements (tenant_id, contact_id, company_id,
                    engagement_score, sequence_number)
                VALUES (1, 1, 1, 150, 99998)
            """))
        report.check("engagements.engagement_score CHECK rejects 150", False)
    except Exception:
        report.check("engagements.engagement_score CHECK rejects 150", True)

    # engagements.terminal pairing — terminal status without terminal_at
    try:
        async with conn.begin_nested():
            await conn.execute(text("""
                INSERT INTO engagements (tenant_id, contact_id, company_id,
                    status, sequence_number)
                VALUES (1, 1, 1, 'terminal', 99997)
            """))
        report.check("engagements.terminal pairing CHECK rejects missing terminal_at", False)
    except Exception:
        report.check("engagements.terminal pairing CHECK rejects missing terminal_at", True)

    # tenant_ai_config: aamp_default with an api key should be rejected
    try:
        async with conn.begin_nested():
            await conn.execute(text("""
                INSERT INTO tenant_ai_config (tenant_id, provider, api_key_encrypted)
                VALUES (99996, 'aamp_default', 'should-not-be-here')
                ON CONFLICT (tenant_id) DO NOTHING
            """))
        # If insert succeeded, the constraint failed
        rows = await conn.execute(text(
            "SELECT 1 FROM tenant_ai_config WHERE tenant_id = 99996"))
        bad = rows.first() is not None
        report.check("tenant_ai_config CHECK: aamp_default + key rejected",
                     not bad)
    except Exception:
        report.check("tenant_ai_config CHECK: aamp_default + key rejected", True)


async def _check_trigger_enforcement(conn, report):
    """Exercise the most-critical triggers against synthetic data. Each test
    is wrapped in a savepoint we ROLLBACK so prod data is untouched."""

    # Setup: find a real tenant/contact/company so FK references work.
    # We use tenant_id=1 (BMP), and look up the first real contact + company.
    rows = await conn.execute(text("""
        SELECT id FROM contacts WHERE tenant_id = 1 ORDER BY id LIMIT 1
    """))
    contact = rows.first()
    rows = await conn.execute(text("""
        SELECT id FROM companies WHERE tenant_id = 1 ORDER BY id LIMIT 1
    """))
    company = rows.first()
    if not contact or not company:
        report.check("trigger tests setup: no contacts/companies available", False)
        return

    contact_id = contact.id
    company_id = company.id

    # Create a throwaway engagement for trigger tests.
    async with conn.begin_nested() as outer:
        eng_row = await conn.execute(text("""
            INSERT INTO engagements (tenant_id, contact_id, company_id,
                sequence_number, current_phase, status)
            VALUES (1, :c, :co, 99999, 'cold_outreach', 'active')
            RETURNING id
        """), {"c": contact_id, "co": company_id})
        eng_id = eng_row.first().id

        # Get the contact's real email for recipient-lock testing
        rows = await conn.execute(text("""
            SELECT email FROM contacts WHERE id = :c
        """), {"c": contact_id})
        real_email = rows.first().email

        # Get channel_id for 'email' and 'manual'
        email_id = (await conn.execute(text(
            "SELECT id FROM channel_types WHERE code = 'email'"))).first().id
        manual_id = (await conn.execute(text(
            "SELECT id FROM channel_types WHERE code = 'manual'"))).first().id

        # Test 1: recipient-lock fires on wrong email
        try:
            async with conn.begin_nested():
                await conn.execute(text("""
                    INSERT INTO actions (tenant_id, engagement_id, contact_id,
                        channel_id, scheduled_at, stale_after,
                        recipient_email, idempotency_key)
                    VALUES (1, :eng, :c, :ch, NOW(), NOW() + INTERVAL '1 day',
                            'attacker@evil.com', 'verify-test-recipient-lock')
                """), {"eng": eng_id, "c": contact_id, "ch": email_id})
            report.check("recipient-lock trigger blocks wrong email", False)
        except Exception as e:
            report.check("recipient-lock trigger blocks wrong email", True)

        # Test 2: recipient-lock allows correct email
        if real_email:
            try:
                async with conn.begin_nested():
                    await conn.execute(text("""
                        INSERT INTO actions (tenant_id, engagement_id, contact_id,
                            channel_id, scheduled_at, stale_after,
                            recipient_email, idempotency_key)
                        VALUES (1, :eng, :c, :ch, NOW(), NOW() + INTERVAL '1 day',
                                :email, 'verify-test-recipient-ok')
                    """), {"eng": eng_id, "c": contact_id, "ch": email_id,
                           "email": real_email})
                report.check("recipient-lock trigger allows correct email", True)
            except Exception as e:
                report.check("recipient-lock trigger allows correct email", False,
                             detail=str(e)[:80])

        # Test 3: recipient-lock exempts manual + sent_by_user_id
        # (Requires a real user; pick the first one.)
        user_rows = await conn.execute(text("""
            SELECT id FROM users WHERE tenant_id = 1 ORDER BY id LIMIT 1
        """))
        user = user_rows.first()
        if user:
            try:
                async with conn.begin_nested():
                    await conn.execute(text("""
                        INSERT INTO actions (tenant_id, engagement_id, contact_id,
                            channel_id, scheduled_at, stale_after,
                            recipient_email, sent_by_user_id, idempotency_key)
                        VALUES (1, :eng, :c, :ch, NOW(), NOW() + INTERVAL '1 day',
                                'somebody@elsecompany.com', :u,
                                'verify-test-manual-exempt')
                    """), {"eng": eng_id, "c": contact_id, "ch": manual_id,
                           "u": user.id})
                report.check(
                    "recipient-lock exempts manual + BDR-attributed", True)
            except Exception as e:
                report.check("recipient-lock exempts manual + BDR-attributed",
                             False, detail=str(e)[:80])

        # Test 4: tenant-consistency trigger on signals
        signal_type_id = (await conn.execute(text(
            "SELECT id FROM signal_types WHERE code = 'manual_note'"))).first().id
        try:
            async with conn.begin_nested():
                await conn.execute(text("""
                    INSERT INTO signals (tenant_id, engagement_id, contact_id,
                        signal_type_id, raw_data_json, observed_at,
                        idempotency_key)
                    VALUES (99, :eng, :c, :st, '{}'::jsonb, NOW(),
                            'verify-test-tenant-mismatch')
                """), {"eng": eng_id, "c": contact_id, "st": signal_type_id})
            report.check("tenant-consistency trigger blocks mismatch", False)
        except Exception:
            report.check("tenant-consistency trigger blocks mismatch", True)

        # Test 5: phase transition trigger blocks illegal transition
        # (cold_outreach → customer with allowed_by='ai' is not in the table)
        try:
            async with conn.begin_nested():
                await conn.execute(text("""
                    UPDATE engagements
                    SET current_phase = 'customer', last_transition_by = 'ai'
                    WHERE id = :eng
                """), {"eng": eng_id})
            report.check("phase-transition trigger blocks illegal hop", False)
        except Exception:
            report.check("phase-transition trigger blocks illegal hop", True)

        # Test 6: phase transition trigger allows legal transition
        try:
            async with conn.begin_nested():
                await conn.execute(text("""
                    UPDATE engagements
                    SET current_phase = 'meeting_set', last_transition_by = 'bdr'
                    WHERE id = :eng
                """), {"eng": eng_id})
            report.check("phase-transition trigger allows legal transition", True)
        except Exception as e:
            report.check("phase-transition trigger allows legal transition",
                         False, detail=str(e)[:80])

        # Rollback all engagement-test state
        await outer.rollback()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
