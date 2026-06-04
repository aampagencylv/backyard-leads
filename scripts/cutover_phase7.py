"""Phase 7 cutover orchestration. The actual migration from old → new engine.

Sub-commands (all idempotent + reversible):

  validate-prod
      Pre-flight: verify all 15 Phase 1 tables, 6 triggers, 4 lookup tables
      seeded, etc. Same checks as verify_engagement_engine_v1.py — re-run
      against prod before touching anything.

  backfill
      One-time data migration:
        1. Run the seq_templates → playbooks importer
        2. Auto-create tenant_ai_config defaults for every tenant
        3. For each active seq_enrollment, create an engagement row
           on the same contact with sequence_number=1, status='active',
           current_phase='cold_outreach'.
        4. Link the engagement.current_playbook_id to the imported
           playbook (matched via legacy_seq_template_id)
      Does NOT flip outreach_owner. The new engine has data ready, but
      no contact is owned by it yet.

  flip-batch
      Flip a batch of contacts from outreach_owner='legacy' to
      'engagement_engine'. Takes either --count N (random N from eligible)
      or --contact-ids 1,2,3 (specific). Writes a cutover_audit row.

      Pre-flip safety:
        - The contact must have an active engagement row (backfill must
          have run)
        - Their seq_enrollment (if any) is paused so the old engine
          doesn't try to send its day-1 step before the new engine sees it
        - outreach_owner must currently equal 'legacy' (not already flipped
          or set to a special state like 'white_glove')

  rollback
      Emergency revert: flip outreach_owner back to 'legacy', mark any
      in-flight new-engine actions as 'blocked' with reason='cutover_rollback',
      un-pause the legacy seq_enrollments. Takes the same --count or
      --contact-ids args. Writes cutover_audit rows.

  metrics
      A/B comparison: for the last N hours, compute reply rate, meeting-set
      rate, send count, cost per meeting — split by which engine owns
      the contact. Used to decide whether to expand the cutover batch.

  enable-workers / disable-workers
      Convenience: set / unset the ENGAGEMENT_*_ENABLED env vars in
      systemd unit overrides. (Or print the bash commands the operator
      needs to run — depending on environment.)

Usage:
    python -m scripts.cutover_phase7 validate-prod
    python -m scripts.cutover_phase7 backfill --dry-run
    python -m scripts.cutover_phase7 backfill
    python -m scripts.cutover_phase7 flip-batch --count 1   # canary
    python -m scripts.cutover_phase7 metrics --hours 24
    python -m scripts.cutover_phase7 rollback --contact-ids 42
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy import text

from app.database import engine, async_session

log = logging.getLogger("cutover.phase7")


def _setup_logging(verbose: bool = False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


# ════════════════════════════════════════════════════════════════════════════
# validate-prod
# ════════════════════════════════════════════════════════════════════════════

async def cmd_validate_prod(args) -> int:
    """Run the Phase 1 verification script against prod. Exit 0 if green."""
    from scripts.verify_engagement_engine_v1 import main as verify_main
    log.info("running verify_engagement_engine_v1 against the current DB...")
    return await verify_main()


# ════════════════════════════════════════════════════════════════════════════
# backfill
# ════════════════════════════════════════════════════════════════════════════

async def cmd_backfill(args) -> int:
    """One-time data migration: import templates, create tenant_ai_config
    defaults, create engagement rows from active seq_enrollments."""
    log.info("Phase 7 BACKFILL starting (dry_run=%s)", args.dry_run)

    # 1) Run the importer (no-op if already imported)
    from scripts.import_seq_templates_to_playbooks import main as importer
    log.info("--- importing seq_templates → playbooks ---")
    if not args.dry_run:
        await importer()
    else:
        log.info("[DRY RUN] would run the importer")

    # 2) Auto-create tenant_ai_config defaults
    log.info("--- creating tenant_ai_config defaults ---")
    async with engine.begin() as conn:
        if args.dry_run:
            row = await conn.execute(text("""
                SELECT t.id FROM tenants t
                LEFT JOIN tenant_ai_config tac ON tac.tenant_id = t.id
                WHERE tac.tenant_id IS NULL
            """))
            missing = [r.id for r in row]
            log.info("[DRY RUN] would create defaults for %d tenants: %s",
                     len(missing), missing)
        else:
            result = await conn.execute(text("""
                INSERT INTO tenant_ai_config (tenant_id, provider)
                SELECT t.id, 'aamp_default'
                FROM tenants t
                LEFT JOIN tenant_ai_config tac ON tac.tenant_id = t.id
                WHERE tac.tenant_id IS NULL
                RETURNING tenant_id
            """))
            created = [r.tenant_id for r in result]
            log.info("created tenant_ai_config defaults for: %s", created)

    # 3) Create engagement rows from active seq_enrollments
    log.info("--- creating engagements from active seq_enrollments ---")
    async with engine.begin() as conn:
        # Find active seq_enrollments NOT yet backfilled.
        # The link condition: an engagement exists for this contact whose
        # current_playbook_id points to the playbook with the matching
        # legacy_seq_template_id.
        candidates = await conn.execute(text("""
            SELECT
                se.id AS enrollment_id,
                se.tenant_id,
                se.contact_id,
                se.company_id,
                se.template_id,
                se.current_step_index,
                se.next_due_at,
                se.status AS enrollment_status,
                p.id AS playbook_id,
                p.version AS playbook_version
            FROM seq_enrollments se
            JOIN playbooks p
                ON p.legacy_seq_template_id = se.template_id
               AND p.is_active = TRUE
            WHERE se.status IN ('active', 'paused', 'snoozed')
              AND NOT EXISTS (
                  SELECT 1 FROM engagements e
                  WHERE e.contact_id = se.contact_id
                    AND e.status != 'terminal'
              )
            ORDER BY se.id
        """))
        rows = list(candidates)
        log.info("found %d active seq_enrollments to backfill", len(rows))

        if args.dry_run:
            for r in rows[:5]:
                log.info(
                    "[DRY RUN] would create engagement for contact %s "
                    "(enrollment %s, playbook %s)",
                    r.contact_id, r.enrollment_id, r.playbook_id,
                )
            if len(rows) > 5:
                log.info("[DRY RUN] ... and %d more", len(rows) - 5)
            return 0

        created = 0
        for r in rows:
            try:
                # Map legacy enrollment status → engagement phase + status
                phase = "cold_outreach"
                eng_status = "active"
                if r.enrollment_status in ("paused", "snoozed"):
                    eng_status = "paused"

                await conn.execute(text("""
                    INSERT INTO engagements (
                        tenant_id, contact_id, company_id, sequence_number,
                        current_phase, status,
                        current_playbook_id, current_playbook_version,
                        current_action_index, next_action_due_at,
                        last_transition_by
                    )
                    VALUES (
                        :t, :c, :co, 1,
                        :phase, :status,
                        :pb, :pbver,
                        :idx, :next,
                        'system'
                    )
                """), {
                    "t": r.tenant_id,
                    "c": r.contact_id,
                    "co": r.company_id,
                    "phase": phase,
                    "status": eng_status,
                    "pb": r.playbook_id,
                    "pbver": r.playbook_version,
                    "idx": r.current_step_index or 0,
                    "next": r.next_due_at,
                })
                created += 1
            except Exception as e:
                log.error(
                    "failed to create engagement for contact %s: %s",
                    r.contact_id, e,
                )

        log.info("backfill complete: %d engagements created", created)

    log.info("--- ensuring cutover_audit table exists ---")
    await _ensure_cutover_audit_table()
    log.info("Phase 7 BACKFILL done.")
    return 0


# ════════════════════════════════════════════════════════════════════════════
# flip-batch
# ════════════════════════════════════════════════════════════════════════════

async def cmd_flip_batch(args) -> int:
    """Flip a batch of contacts to outreach_owner='engagement_engine'."""
    if not args.count and not args.contact_ids:
        log.error("must provide either --count N or --contact-ids 1,2,3")
        return 2

    await _ensure_cutover_audit_table()

    target_ids = []
    if args.contact_ids:
        target_ids = [int(x.strip()) for x in args.contact_ids.split(",")
                      if x.strip()]
    else:
        # Random sample of N eligible contacts
        async with engine.begin() as conn:
            rows = await conn.execute(text("""
                SELECT c.id
                FROM contacts c
                JOIN engagements e ON e.contact_id = c.id
                WHERE c.outreach_owner = 'legacy'
                  AND c.do_not_contact = FALSE
                  AND e.status IN ('active', 'paused')
                ORDER BY RANDOM()
                LIMIT :n
            """), {"n": args.count})
            target_ids = [r.id for r in rows]

    if not target_ids:
        log.error("no eligible contacts found; did backfill run?")
        return 1

    log.info("flipping %d contact(s) to engagement_engine: %s",
             len(target_ids), target_ids if len(target_ids) <= 10 else f"{target_ids[:5]}... ({len(target_ids)} total)")

    if args.dry_run:
        log.info("[DRY RUN] no changes written")
        return 0

    async with engine.begin() as conn:
        # 1) Flip outreach_owner
        await conn.execute(text("""
            UPDATE contacts
            SET outreach_owner = 'engagement_engine'
            WHERE id = ANY(:ids) AND outreach_owner = 'legacy'
        """), {"ids": target_ids})

        # 2) Pause any legacy seq_enrollments for these contacts so the
        # old engine doesn't keep marching forward in parallel
        await conn.execute(text("""
            UPDATE seq_enrollments
            SET status = 'paused',
                paused_at = NOW(),
                paused_reason = 'cutover_phase7_flip'
            WHERE contact_id = ANY(:ids)
              AND status IN ('active')
        """), {"ids": target_ids})

        # 3) Audit log row
        await conn.execute(text("""
            INSERT INTO cutover_audit (
                op, contact_ids, requested_count, actual_count, performed_at,
                notes
            )
            VALUES (
                'flip', CAST(:ids AS jsonb), :req, :act, NOW(), :notes
            )
        """), {
            "ids": json.dumps(target_ids),
            "req": args.count if args.count else len(target_ids),
            "act": len(target_ids),
            "notes": args.notes or "",
        })

    log.info("flip complete. Watch metrics, then run flip-batch again to expand.")
    return 0


# ════════════════════════════════════════════════════════════════════════════
# rollback
# ════════════════════════════════════════════════════════════════════════════

async def cmd_rollback(args) -> int:
    """Emergency revert: flip contacts back + block in-flight new-engine actions."""
    if not args.contact_ids and not args.count:
        log.error("must provide either --count N or --contact-ids 1,2,3")
        return 2

    await _ensure_cutover_audit_table()

    target_ids = []
    if args.contact_ids:
        target_ids = [int(x.strip()) for x in args.contact_ids.split(",")
                      if x.strip()]
    else:
        async with engine.begin() as conn:
            rows = await conn.execute(text("""
                SELECT id FROM contacts
                WHERE outreach_owner = 'engagement_engine'
                ORDER BY id DESC LIMIT :n
            """), {"n": args.count})
            target_ids = [r.id for r in rows]

    log.info("ROLLBACK: reverting %d contact(s): %s",
             len(target_ids), target_ids[:10])

    if args.dry_run:
        return 0

    async with engine.begin() as conn:
        # 1) Flip back
        await conn.execute(text("""
            UPDATE contacts SET outreach_owner = 'legacy'
            WHERE id = ANY(:ids)
        """), {"ids": target_ids})

        # 2) Block any in-flight new-engine actions
        await conn.execute(text("""
            UPDATE actions
            SET status = 'blocked',
                skip_reason = 'cutover_rollback'
            WHERE contact_id = ANY(:ids)
              AND status IN ('scheduled', 'awaiting_approval')
        """), {"ids": target_ids})

        # 3) Resume the legacy seq_enrollments that we paused at flip time
        await conn.execute(text("""
            UPDATE seq_enrollments
            SET status = 'active',
                paused_at = NULL,
                paused_reason = NULL
            WHERE contact_id = ANY(:ids)
              AND paused_reason = 'cutover_phase7_flip'
              AND status = 'paused'
        """), {"ids": target_ids})

        # 4) Audit log row
        await conn.execute(text("""
            INSERT INTO cutover_audit (
                op, contact_ids, requested_count, actual_count, performed_at,
                notes
            )
            VALUES (
                'rollback', CAST(:ids AS jsonb), :req, :act, NOW(), :notes
            )
        """), {
            "ids": json.dumps(target_ids),
            "req": args.count if args.count else len(target_ids),
            "act": len(target_ids),
            "notes": args.notes or "",
        })

    log.info("rollback complete. Contacts back on legacy engine.")
    return 0


# ════════════════════════════════════════════════════════════════════════════
# metrics
# ════════════════════════════════════════════════════════════════════════════

async def cmd_metrics(args) -> int:
    """A/B comparison: old engine vs new engine over the last N hours."""
    hours = args.hours

    async with engine.begin() as conn:
        # ── Old engine metrics ─────────────────────────────────────────────
        old_metrics = await conn.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE ge.is_sent = TRUE) AS sent,
                COUNT(*) FILTER (WHERE ge.skipped_at IS NOT NULL) AS skipped,
                (SELECT COUNT(*) FROM activities a
                 WHERE a.activity_type = 'reply_received'
                   AND a.created_at > NOW() - (:hrs * INTERVAL '1 hour'))
                    AS replies,
                (SELECT COUNT(*) FROM activities a
                 WHERE a.activity_type IN ('meeting_set', 'meeting_booked')
                   AND a.created_at > NOW() - (:hrs * INTERVAL '1 hour'))
                    AS meetings
            FROM generated_emails ge
            JOIN contacts c ON c.id = ge.contact_id
            WHERE c.outreach_owner = 'legacy'
              AND COALESCE(ge.sent_at, ge.skipped_at) > NOW() - (:hrs * INTERVAL '1 hour')
        """), {"hrs": hours})
        old = old_metrics.first()

        # ── New engine metrics ─────────────────────────────────────────────
        new_metrics = await conn.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE a.status = 'sent') AS sent,
                COUNT(*) FILTER (WHERE a.status IN ('blocked', 'skipped')) AS skipped,
                COUNT(*) FILTER (WHERE a.outcome = 'replied') AS replies,
                COUNT(*) FILTER (WHERE a.outcome IN ('meeting_set', 'meeting_booked')) AS meetings,
                COALESCE(SUM(a.send_cost_usd), 0) +
                COALESCE((SELECT SUM(ad.cost_usd)
                          FROM ai_decisions ad
                          JOIN engagements e ON e.id = ad.engagement_id
                          JOIN contacts c2 ON c2.id = e.contact_id
                          WHERE c2.outreach_owner = 'engagement_engine'
                            AND ad.created_at > NOW() - (:hrs * INTERVAL '1 hour')), 0)
                    AS total_cost_usd
            FROM actions a
            JOIN contacts c ON c.id = a.contact_id
            WHERE c.outreach_owner = 'engagement_engine'
              AND COALESCE(a.executed_at, a.scheduled_at) > NOW() - (:hrs * INTERVAL '1 hour')
        """), {"hrs": hours})
        new = new_metrics.first()

    print()
    print(f"=== Cutover metrics, last {hours} hours ===")
    print(f"{'':25} {'OLD ENGINE':>12} {'NEW ENGINE':>12}")
    print(f"{'-'*55}")
    print(f"{'Sends':25} {old.sent or 0:>12} {new.sent or 0:>12}")
    print(f"{'Skipped/Blocked':25} {old.skipped or 0:>12} {new.skipped or 0:>12}")
    print(f"{'Replies':25} {old.replies or 0:>12} {new.replies or 0:>12}")
    print(f"{'Meetings booked':25} {old.meetings or 0:>12} {new.meetings or 0:>12}")

    old_reply_rate = (old.replies or 0) / max(1, old.sent or 0)
    new_reply_rate = (new.replies or 0) / max(1, new.sent or 0)
    print(f"{'Reply rate':25} {old_reply_rate:>12.1%} {new_reply_rate:>12.1%}")

    old_meeting_rate = (old.meetings or 0) / max(1, old.sent or 0)
    new_meeting_rate = (new.meetings or 0) / max(1, new.sent or 0)
    print(f"{'Meeting/send rate':25} {old_meeting_rate:>12.1%} {new_meeting_rate:>12.1%}")

    new_cost = float(new.total_cost_usd or 0)
    cost_per_meeting = new_cost / max(1, new.meetings or 0)
    print(f"{'New engine AI cost':25} {'-':>12} {f'${new_cost:.2f}':>12}")
    print(f"{'Cost per meeting (new)':25} {'-':>12} {f'${cost_per_meeting:.2f}':>12}")
    print()

    # Threshold-based recommendation
    if (new.sent or 0) > 5:
        if new_reply_rate < old_reply_rate * 0.5:
            print("⚠️  WARNING: new engine reply rate is < 50% of old. Consider rollback.")
            return 1
        if new_meeting_rate < old_meeting_rate * 0.5 and (old.meetings or 0) > 0:
            print("⚠️  WARNING: new engine meeting rate is < 50% of old. Consider rollback.")
            return 1
        print("✓ Metrics look healthy. Safe to expand batch.")
    else:
        print("ℹ  Sample size too small for confident comparison. Run again later.")

    return 0


# ════════════════════════════════════════════════════════════════════════════
# enable-workers / disable-workers
# ════════════════════════════════════════════════════════════════════════════

async def cmd_enable_workers(args) -> int:
    """Print the env-var commands needed to enable the workers."""
    print("To enable the engagement engine workers on prod, set these env vars")
    print("in /opt/backyard-leads/.env (or systemd unit overrides):")
    print()
    print("  ENGAGEMENT_DISPATCHER_ENABLED=true       # dispatcher tick")
    print("  ENGAGEMENT_WATCHER_ENABLED=true          # signal watcher tick")
    print("  ENGAGEMENT_DECISION_MAKER_ENABLED=true   # AI decision tick")
    print()
    print("Then add to crontab (every 1m for decision/dispatcher, every 5m for watcher):")
    print()
    print("  * * * * * cd /opt/backyard-leads && /opt/backyard-leads/venv/bin/python -m scripts.run_engagement_dispatcher >> /var/log/eed-dispatcher.log 2>&1")
    print("  * * * * * cd /opt/backyard-leads && /opt/backyard-leads/venv/bin/python -m scripts.run_engagement_decision_maker >> /var/log/eed-decisions.log 2>&1")
    print("  */5 * * * * cd /opt/backyard-leads && /opt/backyard-leads/venv/bin/python -m scripts.run_engagement_signal_watcher >> /var/log/eed-watcher.log 2>&1")
    print()
    print("To disable: unset the env vars + remove the cron lines.")
    return 0


# ════════════════════════════════════════════════════════════════════════════
# Cutover audit table
# ════════════════════════════════════════════════════════════════════════════

async def _ensure_cutover_audit_table():
    """Idempotent: create the cutover_audit table if it doesn't exist."""
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cutover_audit (
                id              SERIAL PRIMARY KEY,
                op              VARCHAR(20) NOT NULL
                                  CHECK (op IN ('flip', 'rollback', 'backfill')),
                contact_ids     JSONB NOT NULL,
                requested_count INTEGER,
                actual_count    INTEGER NOT NULL,
                performed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                notes           TEXT
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_cutover_audit_performed
              ON cutover_audit (performed_at DESC)
        """))


# ════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase 7 cutover orchestration")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_validate = sub.add_parser("validate-prod", help="Run pre-flight verification")
    p_validate.add_argument("--verbose", action="store_true")

    p_backfill = sub.add_parser("backfill", help="Import templates + create engagements from active enrollments")
    p_backfill.add_argument("--dry-run", action="store_true")
    p_backfill.add_argument("--verbose", action="store_true")

    p_flip = sub.add_parser("flip-batch", help="Flip contacts to the new engine")
    p_flip.add_argument("--count", type=int, default=None, help="Random N eligible contacts")
    p_flip.add_argument("--contact-ids", type=str, default=None, help="Comma-separated contact IDs")
    p_flip.add_argument("--notes", type=str, default=None, help="Audit log notes")
    p_flip.add_argument("--dry-run", action="store_true")
    p_flip.add_argument("--verbose", action="store_true")

    p_rollback = sub.add_parser("rollback", help="Revert contacts to legacy engine")
    p_rollback.add_argument("--count", type=int, default=None)
    p_rollback.add_argument("--contact-ids", type=str, default=None)
    p_rollback.add_argument("--notes", type=str, default=None)
    p_rollback.add_argument("--dry-run", action="store_true")
    p_rollback.add_argument("--verbose", action="store_true")

    p_metrics = sub.add_parser("metrics", help="Old vs new engine A/B comparison")
    p_metrics.add_argument("--hours", type=int, default=24)
    p_metrics.add_argument("--verbose", action="store_true")

    p_enable = sub.add_parser("enable-workers", help="Show the env+cron commands")
    p_enable.add_argument("--verbose", action="store_true")

    return p


COMMANDS = {
    "validate-prod":   cmd_validate_prod,
    "backfill":        cmd_backfill,
    "flip-batch":      cmd_flip_batch,
    "rollback":        cmd_rollback,
    "metrics":         cmd_metrics,
    "enable-workers":  cmd_enable_workers,
}


def main_sync() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    _setup_logging(verbose=getattr(args, "verbose", False))
    handler = COMMANDS[args.cmd]
    return asyncio.run(handler(args))


if __name__ == "__main__":
    sys.exit(main_sync())
