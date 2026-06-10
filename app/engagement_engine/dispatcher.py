"""Action Dispatcher — the worker that actually sends actions.

One tick of this worker:
  1. Fetch up to N actions with status='scheduled' AND scheduled_at <= NOW()
     using SELECT ... FOR UPDATE SKIP LOCKED so multiple dispatcher instances
     get disjoint sets of rows.
  2. For each action:
     a. Run kill-switch gates (kill_switches.check_dispatch_eligibility)
     b. Look up the channel adapter via the registry
     c. Compute contact-local-time and check is_in_send_window()
     d. Run channel.pre_dispatch_guards() (suppression, warmup, etc.)
     e. Update dispatch_heartbeat_at + dispatch_worker_id (so crash recovery
        can find abandoned actions after 60s of no heartbeat)
     f. Call channel.send()
     g. Update action with result (sent / failed / transient → reschedule)
     h. Update engagement.last_outreach_at on success
     i. Write to the legacy outbound audit log so existing dashboards see it

The tick is designed to be safe to call concurrently across multiple
worker processes. Per-action transactions + SKIP LOCKED + the
UNIQUE(idempotency_key) constraint together guarantee that no action
is sent twice even under concurrent / crash / retry scenarios.

Runtime mode: importable function for an in-process scheduler, OR
invokable via `python -m scripts.run_engagement_dispatcher` as a cron task
that runs one tick then exits.

PHASE 2 SCOPE: dispatcher reads scheduled actions and dispatches them.
Generation of NEW actions (from signals or playbook timers) is the job of
the decision_maker (Phase 4) and signal_watcher (Phase 3). Until those
ship, the dispatcher tick will simply find zero scheduled actions and
return immediately — safe to enable in shadow mode.
"""
from __future__ import annotations
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.engagement_engine.channels import get_channel, supported_channels
from app.engagement_engine.interfaces import (
    GuardResult,
    SendResult,
    TransientChannelError,
    PermanentChannelError,
)
from app.engagement_engine.kill_switches import check_dispatch_eligibility

log = logging.getLogger("engagement_engine.dispatcher")


# How many actions to claim per tick. Tuned for a single uvicorn worker
# running every 30 seconds; a 4-worker fleet at 30s tick handles
# ~20 actions × 4 = 80 actions/30s = 9.6k/hour ceiling, well above
# BMP's projected volume.
DISPATCH_BATCH_SIZE = 20

# How long to wait before retrying a transient failure. The action's
# scheduled_at gets bumped this far into the future.
TRANSIENT_RETRY_DELAY_SECONDS = 300

# An action whose dispatch_heartbeat_at is older than this is considered
# abandoned (worker crashed mid-dispatch) and is eligible for re-pickup
# by another worker. The idempotency_key UNIQUE constraint prevents
# duplicate sends if the original worker also resumes.
ABANDONED_HEARTBEAT_SECONDS = 60


@dataclass
class TickReport:
    """Per-tick metrics shipped to the observability layer."""
    started_at: datetime
    finished_at: datetime | None = None
    fetched: int = 0
    sent: int = 0
    failed: int = 0
    blocked: int = 0
    skipped_stale: int = 0
    skipped_no_adapter: int = 0
    transient_rescheduled: int = 0
    out_of_send_window_rescheduled: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        if self.finished_at is None:
            return 0
        return int((self.finished_at - self.started_at).total_seconds() * 1000)


async def run_dispatcher_tick(
    *, batch_size: int = DISPATCH_BATCH_SIZE, dry_run: bool = False,
) -> TickReport:
    """One tick of the dispatcher worker.

    dry_run=True: all the checks run but `channel.send()` is skipped. The
    action's status stays 'scheduled'. Used for staging/shadow validation
    of the full pipeline before enabling real dispatch.
    """
    report = TickReport(started_at=datetime.now(timezone.utc))
    worker_id = _worker_id()

    try:
        async with async_session() as session:
            # Claim a batch of due actions with SKIP LOCKED. We hold the
            # row lock until commit, but the per-action processing happens
            # outside this transaction — we just need the lock long enough
            # to mark them claimed via heartbeat.
            action_ids = await _claim_due_actions(
                session, batch_size=batch_size, worker_id=worker_id,
            )
            await session.commit()
        report.fetched = len(action_ids)
    except Exception as e:
        report.errors.append(f"claim_failed: {type(e).__name__}: {e}")
        report.finished_at = datetime.now(timezone.utc)
        return report

    for action_id in action_ids:
        try:
            await _process_one_action(
                action_id=action_id,
                worker_id=worker_id,
                report=report,
                dry_run=dry_run,
            )
        except Exception as e:
            report.errors.append(
                f"action {action_id} unhandled: {type(e).__name__}: {e}",
            )
            log.exception(f"dispatcher unhandled exception on action {action_id}")

    report.finished_at = datetime.now(timezone.utc)
    return report


async def dispatch_action_now(action_id: int, *, tenant_id: int | None = None) -> TickReport:
    """Manually dispatch ONE scheduled action immediately.

    This is the path a BDR clicking "Start Sequence" / "Send Next" in the
    CRM takes for an engagement-engine contact (the legacy GeneratedEmail
    send route can't see `actions` rows). It runs the exact same per-action
    pipeline as the background tick — kill-switch gates, send-window check,
    channel guards, idempotent send, Activity dual-write — so a manual send
    and an automatic send are indistinguishable downstream.

    Differences from a tick:
      * It targets one caller-specified action instead of claiming a due
        batch, and it does NOT require scheduled_at <= NOW(): a manual click
        is an explicit override of the step's scheduled date (parity with the
        legacy "Send Next", which never gated on the scheduled date).
      * It still claims via the heartbeat guard so it can't double-send a row
        the background dispatcher is already processing. If the row isn't
        claimable (already sent / in-flight / not 'scheduled'), the returned
        report has fetched=0 and the caller can surface a friendly message.

    `tenant_id`: pass the caller's tenant when invoking from a tenant-scoped
    route. This function runs on a global (non-RLS) session, so the tenant
    filter on the claim is the defense-in-depth check that a route can't be
    coaxed into dispatching another tenant's action id.
    """
    report = TickReport(started_at=datetime.now(timezone.utc))
    worker_id = _worker_id()

    tenant_clause = "AND tenant_id = :tid" if tenant_id is not None else ""
    params = {"wid": worker_id, "id": action_id}
    if tenant_id is not None:
        params["tid"] = tenant_id
    async with async_session() as session:
        claimed = await session.execute(text(f"""
            UPDATE actions
            SET dispatch_heartbeat_at = NOW(),
                dispatch_worker_id    = :wid
            WHERE id = :id
              {tenant_clause}
              AND status = 'scheduled'
              AND (dispatch_heartbeat_at IS NULL
                   OR dispatch_heartbeat_at < NOW() - INTERVAL ':abandoned seconds')
            RETURNING id
        """.replace(":abandoned", str(ABANDONED_HEARTBEAT_SECONDS))), params)
        got = claimed.first()
        await session.commit()

    if got is None:
        # Not claimable: already sent, mid-flight in another worker, or not
        # in 'scheduled' state. fetched stays 0 so the caller can tell.
        report.finished_at = datetime.now(timezone.utc)
        return report

    report.fetched = 1
    try:
        await _process_one_action(
            action_id=action_id, worker_id=worker_id, report=report, dry_run=False,
        )
    except Exception as e:
        report.errors.append(
            f"action {action_id} unhandled: {type(e).__name__}: {e}",
        )
        log.exception(f"manual dispatch unhandled exception on action {action_id}")

    report.finished_at = datetime.now(timezone.utc)
    return report


async def _claim_due_actions(
    session: AsyncSession, *, batch_size: int, worker_id: str,
) -> list[int]:
    """SELECT ... FOR UPDATE SKIP LOCKED, then immediately stamp the
    heartbeat + worker_id so other workers see this action as claimed.
    Returns the list of claimed action IDs."""
    rows = await session.execute(text("""
        SELECT id FROM actions
        WHERE status = 'scheduled'
          AND scheduled_at <= NOW()
          AND (dispatch_heartbeat_at IS NULL
               OR dispatch_heartbeat_at < NOW() - INTERVAL ':abandoned seconds')
        ORDER BY scheduled_at
        LIMIT :n
        FOR UPDATE SKIP LOCKED
    """.replace(":abandoned", str(ABANDONED_HEARTBEAT_SECONDS))),
                                   {"n": batch_size})
    ids = [r.id for r in rows]
    if not ids:
        return []

    # Mark each as claimed via heartbeat. We can do this in one UPDATE
    # because we already hold FOR UPDATE locks on them.
    await session.execute(text("""
        UPDATE actions
        SET dispatch_heartbeat_at = NOW(),
            dispatch_worker_id    = :wid
        WHERE id = ANY(:ids)
    """), {"wid": worker_id, "ids": ids})

    return ids


async def _process_one_action(
    *, action_id: int, worker_id: str, report: TickReport, dry_run: bool,
) -> None:
    """Single-action processing path. Each action gets its own session +
    transaction so failures don't poison the rest of the batch."""

    # 1. Run kill-switch + stale-action + recipient-drift checks
    async with async_session() as session:
        conn = await session.connection()
        eligibility = await check_dispatch_eligibility(
            conn, action_id=action_id,
        )
        if not eligibility.eligible:
            # company_snoozed is TEMPORAL — the BDR said "not until date X",
            # not "never". Reschedule to the snooze-end instead of blocking,
            # so the sequence picks back up when the snooze lapses.
            if eligibility.block_reason == "company_snoozed":
                await session.execute(text("""
                    UPDATE actions a
                    SET scheduled_at = GREATEST(
                            co.sequence_resume_at,
                            NOW() + INTERVAL '1 hour'),
                        stale_after = GREATEST(
                            co.sequence_resume_at,
                            NOW() + INTERVAL '1 hour') + (a.stale_after - a.scheduled_at),
                        dispatch_heartbeat_at = NULL,
                        dispatch_worker_id = NULL,
                        updated_at = NOW()
                    FROM engagements e
                    JOIN companies co ON co.id = e.company_id
                    WHERE e.id = a.engagement_id AND a.id = :id
                """), {"id": action_id})
                await session.commit()
                report.out_of_send_window_rescheduled += 1
                return
            await _mark_action(
                session, action_id,
                status="skipped" if "stale" in eligibility.block_reason
                          else "blocked",
                skip_reason=eligibility.block_reason,
            )
            await session.commit()
            if "stale" in eligibility.block_reason:
                report.skipped_stale += 1
            else:
                report.blocked += 1
            return

    # 2. Load action + contact + channel for further processing
    async with async_session() as session:
        row = await session.execute(text("""
            SELECT
                a.id, a.tenant_id, a.engagement_id, a.contact_id,
                a.channel_id, a.subject, a.body, a.task_description,
                a.recipient_email, a.recipient_phone, a.recipient_linkedin_url,
                a.scheduled_at, a.contact_timezone,
                ct.code AS channel_code,
                tac.tcpa_b2b_override,
                tac.default_timezone AS tenant_default_timezone
            FROM actions a
            JOIN channel_types ct ON ct.id = a.channel_id
            LEFT JOIN tenant_ai_config tac ON tac.tenant_id = a.tenant_id
            WHERE a.id = :id
        """), {"id": action_id})
        record = row.first()
        if record is None:
            report.errors.append(f"action {action_id} disappeared mid-tick")
            return

    channel_code = record.channel_code

    # 3. Look up the channel adapter
    adapter = get_channel(channel_code)
    if adapter is None:
        async with async_session() as session:
            await _mark_action(
                session, action_id,
                status="failed",
                skip_reason=f"no_adapter:{channel_code}",
                error_message=(
                    f"no channel adapter registered for {channel_code}; "
                    f"supported: {supported_channels()}"
                ),
            )
            await session.commit()
        report.skipped_no_adapter += 1
        return

    # 4. Send-window check (TZ-aware)
    local_now = _compute_local_now(
        record.contact_timezone or record.tenant_default_timezone or "UTC",
    )
    tcpa_override = bool(record.tcpa_b2b_override)
    if not await adapter.is_in_send_window(local_now, tcpa_override):
        # Reschedule to next legal local hour, don't block
        next_legal = _next_legal_send_time(
            adapter, local_now, tcpa_override,
            tz_name=record.contact_timezone or record.tenant_default_timezone or "UTC",
        )
        async with async_session() as session:
            await session.execute(text("""
                UPDATE actions
                SET scheduled_at = :sched,
                    dispatch_heartbeat_at = NULL,
                    dispatch_worker_id = NULL
                WHERE id = :id
            """), {"id": action_id, "sched": next_legal})
            await session.commit()
        report.out_of_send_window_rescheduled += 1
        return

    # 5. Channel-specific pre-dispatch guards
    guard = await adapter.pre_dispatch_guards(record)
    if guard.blocked:
        async with async_session() as session:
            await _mark_action(
                session, action_id,
                status="blocked",
                skip_reason=guard.reason or "guard_blocked",
            )
            await session.commit()
        report.blocked += 1
        return

    # 6. Dispatch (skip in dry_run mode for shadow validation)
    if dry_run:
        async with async_session() as session:
            await session.execute(text("""
                UPDATE actions SET dispatch_heartbeat_at = NULL,
                                   dispatch_worker_id = NULL
                WHERE id = :id
            """), {"id": action_id})
            await session.commit()
        report.sent += 1  # count as 'would have sent'
        return

    # Refresh heartbeat right before send (long-running channel calls
    # otherwise look abandoned and get re-claimed by another worker)
    async with async_session() as session:
        await session.execute(text("""
            UPDATE actions SET dispatch_heartbeat_at = NOW()
            WHERE id = :id
        """), {"id": action_id})
        await session.commit()

    try:
        result = await adapter.send(record)
    except TransientChannelError as e:
        # Reschedule with delay
        async with async_session() as session:
            await session.execute(text("""
                UPDATE actions
                SET scheduled_at = NOW() + INTERVAL ':d seconds',
                    dispatch_heartbeat_at = NULL,
                    dispatch_worker_id = NULL,
                    error_message = :err
                WHERE id = :id
            """.replace(":d", str(TRANSIENT_RETRY_DELAY_SECONDS))),
                                  {"id": action_id, "err": str(e)[:500]})
            await session.commit()
        report.transient_rescheduled += 1
        return
    except PermanentChannelError as e:
        async with async_session() as session:
            await _mark_action(
                session, action_id, status="failed",
                error_message=str(e)[:500],
            )
            await session.commit()
        report.failed += 1
        return

    # 7. Success: update action + engagement
    async with async_session() as session:
        await session.execute(text("""
            UPDATE actions
            SET status = 'sent',
                executed_at = NOW(),
                external_id = :ext,
                send_cost_usd = :cost,
                dispatch_heartbeat_at = NULL,
                dispatch_worker_id = NULL,
                updated_at = NOW()
            WHERE id = :id
        """), {
            "id": action_id,
            "ext": result.external_id,
            "cost": result.cost_usd or 0,
        })
        await session.execute(text("""
            UPDATE engagements
            SET last_outreach_at = NOW(), updated_at = NOW()
            WHERE id = :eng
        """), {"eng": record.engagement_id})
        bdr_row = (await session.execute(text("""
            SELECT COALESCE(e.assigned_bdr_id, co.assigned_to) AS user_id
            FROM engagements e
            JOIN companies co ON co.id = e.company_id
            WHERE e.id = :eng
        """), {"eng": record.engagement_id})).first()
        await session.commit()

    # Billing parity with the legacy send path (which meters every manual
    # send). Engine sends were previously unmetered — free emails/SMS once
    # billing enforcement turns on. Idempotency key is the action id, so
    # crash-recovery re-processing can't double-charge. call_task/manual
    # channels create CRM tasks, not vendor spend — no meter row for those.
    meter_type = {"email": "email_send", "sms": "sms_send"}.get(channel_code)
    if meter_type:
        from app.services.credit_meter import meter_standalone, make_idem_key
        await meter_standalone(
            action_type=meter_type,
            idempotency_key=make_idem_key(meter_type, f"engine_action:{action_id}"),
            user_id=bdr_row.user_id if bdr_row else None,
            action_ref=f"engagement_action:{action_id}",
            raw_cost_override_usd=result.cost_usd,
            tenant_id=record.tenant_id,
        )
    report.sent += 1


# ── Helpers ────────────────────────────────────────────────────────────────

async def _mark_action(
    session, action_id: int, *,
    status: str,
    skip_reason: str | None = None,
    error_message: str | None = None,
) -> None:
    """Status update + clear heartbeat fields. Common to several outcomes."""
    await session.execute(text("""
        UPDATE actions
        SET status = :s,
            skip_reason = :reason,
            error_message = COALESCE(:err, error_message),
            dispatch_heartbeat_at = NULL,
            dispatch_worker_id = NULL,
            updated_at = NOW()
        WHERE id = :id
    """), {
        "id": action_id,
        "s": status,
        "reason": skip_reason,
        "err": error_message,
    })


def _worker_id() -> str:
    """Stable per-process worker identifier for heartbeat ownership."""
    pid = os.getpid()
    return f"dispatcher-{pid}-{uuid.uuid4().hex[:8]}"


def _compute_local_now(tz_name: str) -> datetime:
    """Returns current time in the named IANA timezone. Falls back to UTC
    on invalid timezone string (a malformed contact.timezone shouldn't
    crash the dispatcher)."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(tz_name)
        return datetime.now(tz)
    except Exception:
        log.warning(f"unknown timezone {tz_name!r}, falling back to UTC")
        return datetime.now(timezone.utc)


def _next_legal_send_time(
    adapter, local_now: datetime, tcpa_b2b_override: bool, tz_name: str,
) -> datetime:
    """Find the next UTC instant when the channel will accept a send.

    Steps forward in 1-hour increments (no need for minute precision —
    quiet hours are hour-aligned) and converts back to UTC.
    """
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc

    # Step forward up to 25 hours (covers any wrap-around)
    cursor = local_now
    for _ in range(25):
        cursor = cursor + timedelta(hours=1)
        if _check_send_window_sync(adapter, cursor, tcpa_b2b_override):
            return cursor.astimezone(timezone.utc)
    # Defensive fallback: schedule 8 hours out in UTC
    return datetime.now(timezone.utc) + timedelta(hours=8)


def _check_send_window_sync(adapter, local_dt: datetime, override: bool) -> bool:
    """Sync wrapper for the async is_in_send_window — by design, the
    adapter's window check should be pure-function (no IO), so we can
    call it via .send method as a coroutine if needed. For now, just call
    the rule directly: email 7-22, sms 8-21 (or 7-22 override)."""
    h = local_dt.hour
    code = adapter.channel_code
    if code == "email":
        return 7 <= h < 22
    if code == "sms":
        if override:
            return 7 <= h < 22
        return 8 <= h < 21
    return True  # manual, call_task, linkedin — no window restriction
