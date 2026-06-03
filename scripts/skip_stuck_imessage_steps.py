"""One-shot: mark every currently-stuck iMessage step as skipped.

The sequence engine was attempting iMessage sends repeatedly, failing,
and incrementing error counters without ever advancing the step. That
left ~276 BMP companies frozen on imessage_1 / imessage_2 — the engine
never gave up, so the next step in the sequence never fired.

After landing the toggle + SKIP: convention in sequence_engine.py, new
iMessage steps will auto-skip cleanly. This script handles the backlog:
any iMessage step that is pending (not sent, not skipped, not paused)
gets marked skipped with reason 'imessage_disabled_by_tenant' so the
next step can dispatch on the next engine tick.

Scope: ALL tenants where imessage_enabled is FALSE. Runs once. Idempotent
in practice — already-skipped steps are filtered out by the WHERE clause.

Usage on VPS:
    cd /opt/backyard-leads
    sudo -u backyard /opt/backyard-leads/.venv/bin/python -m scripts.skip_stuck_imessage_steps
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        # Count first so the operator sees what's about to change.
        count_row = await conn.execute(text("""
            SELECT COUNT(*)
            FROM generated_emails ge
            JOIN companies c ON c.id = ge.company_id
            JOIN runtime_config rc ON rc.tenant_id = c.tenant_id
            WHERE ge.step_type = 'imessage'
              AND ge.is_sent = FALSE
              AND ge.skipped_at IS NULL
              AND ge.paused_at IS NULL
              AND COALESCE(rc.imessage_enabled, FALSE) = FALSE
        """))
        n = count_row.scalar() or 0
        print(f"Stuck iMessage steps eligible for skip: {n}")
        if n == 0:
            print("Nothing to do.")
            return

        result = await conn.execute(text("""
            UPDATE generated_emails ge
            SET skipped_at = :now,
                skip_reason = 'imessage_disabled_by_tenant'
            FROM companies c, runtime_config rc
            WHERE ge.company_id = c.id
              AND rc.tenant_id = c.tenant_id
              AND ge.step_type = 'imessage'
              AND ge.is_sent = FALSE
              AND ge.skipped_at IS NULL
              AND ge.paused_at IS NULL
              AND COALESCE(rc.imessage_enabled, FALSE) = FALSE
        """), {"now": now})
        print(f"+ marked {result.rowcount} step(s) as skipped (reason: imessage_disabled_by_tenant)")
    print("Done — next engine tick will dispatch the following step in each sequence.")


if __name__ == "__main__":
    asyncio.run(main())
