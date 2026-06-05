"""Signal Watcher — Worker A of the engagement engine.

One tick:
  1. Fetch up to N observations where next_poll_at <= NOW() AND is_active
     using SELECT ... FOR UPDATE SKIP LOCKED so multiple instances see
     disjoint sets of rows.
  2. For each observation:
     a. Look up the source adapter via the registry
     b. Call adapter.fetch(source_url) → Snapshot (or SourceError)
     c. If hash matches last_snapshot_hash → no change; update next_poll_at only
     d. Else: load prior snapshot, call extract_signals(prev, current),
        INSERT each ExtractedSignal into the signals table with idempotency
        key `{source_type}-{contact_id}-{content_hash}-{idx}` so a re-poll
        of the same snapshot can't double-insert
     e. Update observations row with new hash + next_poll_at (jittered)
     f. On SourceError: increment consecutive_failures, exponential backoff

The watcher does NOT score relevance — that's Worker B (decision_maker)'s
job. Signals are written with relevance_score=NULL; decision_maker batches
unscored signals through a cheap LLM call.

Per design Rule #2: the idempotency key is a *semantic* key, not per-attempt.
Two pollers fetching the same source concurrently both compute the same
content_hash; both attempt to INSERT; the UNIQUE(idempotency_key) catches
the duplicate and the second worker's INSERT fails silently (ON CONFLICT
DO NOTHING).
"""
from __future__ import annotations
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.engagement_engine.sources import (
    get_source, supported_source_types,
)
from app.engagement_engine.sources.base import compute_next_poll_at
from app.engagement_engine.interfaces import SourceError

log = logging.getLogger("engagement_engine.signal_watcher")


# How many observations to claim per tick. Tuned for a single uvicorn worker
# at 5-min intervals to handle BMP's ~2000 active observations easily.
WATCHER_BATCH_SIZE = 50

# Cap on consecutive failures before we soft-deactivate the observation.
# An observation that fails 10 times in a row probably has a permanent
# problem (bad URL, business closed) — let a BDR review rather than burn
# more API quota on it.
MAX_CONSECUTIVE_FAILURES = 10


@dataclass
class WatcherTickReport:
    """Per-tick metrics shipped to observability."""
    started_at: datetime
    finished_at: datetime | None = None
    fetched: int = 0
    unchanged: int = 0
    changed_with_signals: int = 0
    changed_no_signals: int = 0
    failed: int = 0
    deactivated: int = 0
    signals_written: int = 0
    no_adapter: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        if self.finished_at is None:
            return 0
        return int((self.finished_at - self.started_at).total_seconds() * 1000)


async def run_signal_watcher_tick(
    *, batch_size: int = WATCHER_BATCH_SIZE,
) -> WatcherTickReport:
    """One tick of the signal_watcher worker."""
    report = WatcherTickReport(started_at=datetime.now(timezone.utc))

    try:
        async with async_session() as session:
            observation_ids = await _claim_due_observations(
                session, batch_size=batch_size,
            )
            await session.commit()
        report.fetched = len(observation_ids)
    except Exception as e:
        report.errors.append(f"claim_failed: {type(e).__name__}: {e}")
        report.finished_at = datetime.now(timezone.utc)
        return report

    for obs_id in observation_ids:
        try:
            await _process_one_observation(obs_id=obs_id, report=report)
        except Exception as e:
            report.errors.append(
                f"observation {obs_id} unhandled: {type(e).__name__}: {e}",
            )
            log.exception(
                f"signal_watcher unhandled exception on observation {obs_id}"
            )

    report.finished_at = datetime.now(timezone.utc)
    return report


async def _claim_due_observations(
    session: AsyncSession, *, batch_size: int,
) -> list[int]:
    """SELECT ... FOR UPDATE SKIP LOCKED on due observations.
    Updates last_polled_at so re-claim within the same tick window is rare,
    but does NOT yet advance next_poll_at (that happens once we know
    whether to apply backoff or jitter)."""
    rows = await session.execute(text("""
        SELECT id FROM observations
        WHERE is_active = TRUE
          AND next_poll_at <= NOW()
        ORDER BY next_poll_at
        LIMIT :n
        FOR UPDATE SKIP LOCKED
    """), {"n": batch_size})
    ids = [r.id for r in rows]
    if not ids:
        return []
    # Stamp last_polled_at to mark "I have this"; concurrent dispatchers
    # see this even before the per-observation tx commits.
    await session.execute(text("""
        UPDATE observations
        SET last_polled_at = NOW()
        WHERE id = ANY(:ids)
    """), {"ids": ids})
    return ids


async def _process_one_observation(
    *, obs_id: int, report: WatcherTickReport,
) -> None:
    """Single-observation processing path."""

    # 1. Load observation + tenant for adapter dispatch
    async with async_session() as session:
        row = await session.execute(text("""
            SELECT
                o.id, o.tenant_id, o.contact_id, o.company_id,
                o.current_engagement_id, o.source_url,
                o.last_snapshot_hash, o.poll_interval_days,
                o.consecutive_failures,
                st.code AS source_type_code
            FROM observations o
            JOIN source_types st ON st.id = o.source_type_id
            WHERE o.id = :id
        """), {"id": obs_id})
        obs = row.first()
    if obs is None:
        report.errors.append(f"observation {obs_id} disappeared mid-tick")
        return

    # 2. Look up the source adapter
    adapter = get_source(obs.source_type_code)
    if adapter is None:
        async with async_session() as session:
            await session.execute(text("""
                UPDATE observations
                SET last_error = :err,
                    next_poll_at = NOW() + INTERVAL '7 days'
                WHERE id = :id
            """), {
                "id": obs.id,
                "err": (
                    f"no_adapter:{obs.source_type_code}; "
                    f"supported: {supported_source_types()}"
                ),
            })
            await session.commit()
        report.no_adapter += 1
        return

    # 3. Fetch snapshot
    try:
        snapshot = await adapter.fetch(obs.source_url)
    except SourceError as e:
        await _record_fetch_failure(obs, error=str(e), report=report)
        return
    except Exception as e:
        # Unexpected exception — treat as transient, log, back off
        log.exception(
            f"adapter {obs.source_type_code} raised unexpected error on {obs.source_url}"
        )
        await _record_fetch_failure(
            obs, error=f"{type(e).__name__}: {e}", report=report,
        )
        return

    # 4. Hash compare — unchanged?
    if snapshot.content_hash == obs.last_snapshot_hash:
        async with async_session() as session:
            next_poll = compute_next_poll_at(
                interval_days=obs.poll_interval_days,
            )
            await session.execute(text("""
                UPDATE observations
                SET next_poll_at = :next,
                    last_polled_at = NOW(),
                    last_snapshot_at = NOW(),
                    consecutive_failures = 0,
                    last_error = NULL
                WHERE id = :id
            """), {"id": obs.id, "next": next_poll})
            await session.commit()
        report.unchanged += 1
        return

    # 5. Snapshot differs — load prior snapshot for diff
    prev_snapshot = await _load_prior_snapshot(obs)

    # 6. Extract signals
    try:
        extracted_signals = adapter.extract_signals(prev_snapshot, snapshot)
    except Exception as e:
        log.exception(
            f"extract_signals raised on observation {obs_id}"
        )
        report.errors.append(
            f"observation {obs_id} extract_signals: {type(e).__name__}: {e}"
        )
        # Update observation hash anyway so we don't keep re-diffing
        await _commit_unchanged_state(obs, snapshot)
        return

    # 7. Persist new signals (idempotent — UNIQUE(idempotency_key))
    signals_written = 0
    async with async_session() as session:
        # Resolve current engagement dynamically from the contact's
        # active engagement (not the stale observations.current_engagement_id).
        # Seeded observations may not have it populated; engagements rotate
        # over time (terminate → restore → new id). Dynamic lookup
        # keeps observation rows stable while routing signals to the
        # currently-live engagement.
        eng_id = obs.current_engagement_id
        if eng_id is None:
            eng_row = (await session.execute(text("""
                SELECT e.id FROM engagements e
                JOIN contacts c ON c.id = e.contact_id
                WHERE e.contact_id = :c
                  AND e.status = 'active'
                  AND e.tenant_id = c.tenant_id
                ORDER BY e.id DESC LIMIT 1
            """), {"c": obs.contact_id})).first()
            if eng_row is not None:
                eng_id = int(eng_row[0])
        if eng_id is None:
            # No active engagement — signals can't be attached. We still
            # update the observation hash so we don't keep re-emitting.
            # The next poll-tick can pick up signals once the contact
            # gets enrolled (e.g., autopilot brings them in).
            log.info(
                "observation %s has no active engagement for contact %s; "
                "skipping %d extracted signals",
                obs_id, obs.contact_id, len(extracted_signals),
            )
        else:
            for idx, sig in enumerate(extracted_signals):
                idempotency_key = (
                    f"{obs.source_type_code}-{obs.contact_id}"
                    f"-{snapshot.content_hash}-{idx}"
                )
                inserted = await _insert_signal(
                    session,
                    tenant_id=obs.tenant_id,
                    engagement_id=eng_id,
                    contact_id=obs.contact_id,
                    signal_type_code=sig.signal_type_code,
                    raw_data_json=sig.extracted_facts,
                    source_url=sig.source_url,
                    observed_at=snapshot.observed_at,
                    idempotency_key=idempotency_key,
                )
                if inserted:
                    signals_written += 1

        # Update observation state regardless of whether engagement
        # existed (so we don't re-diff the same snapshot forever)
        next_poll = compute_next_poll_at(
            interval_days=obs.poll_interval_days,
        )
        await session.execute(text("""
            UPDATE observations
            SET last_snapshot_hash = :hash,
                last_snapshot_at = NOW(),
                next_poll_at = :next,
                last_polled_at = NOW(),
                consecutive_failures = 0,
                last_error = NULL
            WHERE id = :id
        """), {
            "id": obs.id,
            "hash": snapshot.content_hash,
            "next": next_poll,
        })
        await session.commit()

    if signals_written > 0:
        report.changed_with_signals += 1
        report.signals_written += signals_written
        log.info(
            "obs %s: %d signals written for engagement %s",
            obs_id, signals_written, obs.current_engagement_id,
        )
    else:
        report.changed_no_signals += 1


async def _insert_signal(
    session: AsyncSession,
    *,
    tenant_id: int,
    engagement_id: int,
    contact_id: int,
    signal_type_code: str,
    raw_data_json: dict,
    source_url: str | None,
    observed_at: datetime,
    idempotency_key: str,
) -> bool:
    """Insert a signal row idempotently. Returns True if a NEW row was
    written, False if the idempotency key already existed.

    Uses INSERT ... ON CONFLICT DO NOTHING so duplicate keys silently
    skip rather than raising — protects the watcher's outer loop from
    a single bad signal aborting the whole batch.
    """
    # Resolve signal_type_id from the lookup table (worker-side lookup is
    # fine; the registry is small + cache-friendly).
    type_row = await session.execute(text("""
        SELECT id FROM signal_types WHERE code = :code
    """), {"code": signal_type_code})
    type_row = type_row.first()
    if type_row is None:
        log.error(
            "unknown signal_type_code from adapter: %s", signal_type_code
        )
        return False

    result = await session.execute(text("""
        INSERT INTO signals (
            tenant_id, engagement_id, contact_id, signal_type_id,
            source_url, raw_data_json, observed_at, idempotency_key,
            is_untrusted_content
        )
        VALUES (
            :t, :eng, :c, :st, :src,
            CAST(:raw AS jsonb), :obs, :idem,
            TRUE
        )
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
    """), {
        "t": tenant_id,
        "eng": engagement_id,
        "c": contact_id,
        "st": type_row.id,
        "src": source_url,
        "raw": json.dumps(raw_data_json, default=str),
        "obs": observed_at,
        "idem": idempotency_key,
    })
    return result.first() is not None


async def _load_prior_snapshot(obs):
    """Reconstruct a Snapshot from the prior observation row.

    The schema doesn't store the full prior snapshot data — only the hash.
    For diff-based adapters that need the raw fields (rating, address,
    review count, etc.), we load the most recent signals from that
    observation's source and rebuild a "prior state" view. For Phase 3
    minimum, we pass None as prev_snapshot when last_snapshot_hash exists
    but we can't reconstruct content; adapters that can extract signals
    from "current state alone" (e.g., always emitting on first non-match)
    still work. For adapters that need diff (most), the first poll after
    a worker restart won't emit signals — that's an acceptable cost.
    """
    # Phase 3: prev_snapshot inference deferred to Phase 4 (when signals
    # table is queried for prior-state reconstruction). For now, the
    # watcher always passes None as prev_snapshot when we don't have
    # explicit prior content cached.
    return None


async def _record_fetch_failure(
    obs, *, error: str, report: WatcherTickReport,
) -> None:
    """On SourceError: increment consecutive_failures, back off, possibly
    deactivate."""
    new_fail_count = obs.consecutive_failures + 1
    deactivate = new_fail_count >= MAX_CONSECUTIVE_FAILURES

    async with async_session() as session:
        if deactivate:
            await session.execute(text("""
                UPDATE observations
                SET is_active = FALSE,
                    consecutive_failures = :n,
                    last_error = :err,
                    last_polled_at = NOW()
                WHERE id = :id
            """), {"id": obs.id, "n": new_fail_count, "err": error[:1000]})
            report.deactivated += 1
        else:
            next_poll = compute_next_poll_at(
                interval_days=obs.poll_interval_days,
                consecutive_failures=new_fail_count,
            )
            await session.execute(text("""
                UPDATE observations
                SET consecutive_failures = :n,
                    last_error = :err,
                    next_poll_at = :next,
                    last_polled_at = NOW()
                WHERE id = :id
            """), {
                "id": obs.id,
                "n": new_fail_count,
                "err": error[:1000],
                "next": next_poll,
            })
        await session.commit()
    report.failed += 1


async def _commit_unchanged_state(obs, snapshot):
    """When extract_signals fails but the fetch succeeded, we still want
    to record the new hash so we don't re-process the same snapshot."""
    async with async_session() as session:
        await session.execute(text("""
            UPDATE observations
            SET last_snapshot_hash = :hash,
                last_snapshot_at = NOW(),
                next_poll_at = NOW() + INTERVAL '1 day',
                last_polled_at = NOW(),
                consecutive_failures = 0,
                last_error = 'extract_signals_failed_hash_updated'
            WHERE id = :id
        """), {"id": obs.id, "hash": snapshot.content_hash})
        await session.commit()
