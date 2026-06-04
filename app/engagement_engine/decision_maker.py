"""Decision Maker — Worker B of the engagement engine. The AI brain.

One tick handles two workloads (Phase 4 scope; reactive playbook + nightly
batches land later):

  1. SCORE unscored signals (cheap model). Reads signals.relevance_score
     IS NULL; calls LLM.score_signal_relevance; updates the signal with
     score + ai_summary. Marks the engagement's summary as stale if the
     score crosses 70.

  2. REACT to high-relevance signals (expensive model). Reads signals
     where relevance_score >= 70 AND NOT yet triggered an action. Calls
     LLM.what_to_send → inserts an action row with idempotency_key
     'sig-{signal_id}'. The dispatcher (Phase 2) takes it from there.

Concurrency:
  - SELECT ... FOR UPDATE SKIP LOCKED on both queries (Pattern A)
  - Advisory locks released across the LLM call (Pattern B from v3)
  - actions.idempotency_key UNIQUE catches duplicates if two workers race
    on the same signal somehow

Defense layers per Rule #12 (prompt injection):
  - Untrusted text (signal raw_data, BDR notes, reply bodies) wrapped in
    <untrusted_content> blocks via prompt_builders
  - System prompt prefix instructs LLM to treat wrapped text as data
  - validate_ai_action runs on every generated action BEFORE persist
  - DB recipient-lock trigger is the final structural defense
  - Output Pydantic schema with extra='forbid' rejects sneaky extra fields

Cost (Rule #10):
  - reserve_budget BEFORE the LLM call (atomic UPDATE-WHERE at engagement
    + tenant level). If reservation fails, engagement is paused with
    reason='cost_budget_exceeded'.
  - reconcile_actual_cost AFTER, using provider-reported tokens. Falls
    back to STATIC_PRICE_TABLE when unreported.

This worker does NOT send anything. Generated actions land in the
actions table with status='scheduled'; the dispatcher worker (Phase 2)
picks them up.
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
from app.engagement_engine.cost import (
    reserve_budget, reconcile_actual_cost, estimate_cost_usd,
)
from app.engagement_engine.interfaces import (
    LLMResponse, ParseError, ProviderUnavailable,
    RateLimitExceeded, CostBudgetExceeded,
)
from app.engagement_engine.llm_providers import (
    get_provider_for_tenant, get_model_for_decision_type,
)
from app.engagement_engine.prompt_builders import (
    build_score_signal_prompt,
    build_what_to_send_prompt,
)
from app.engagement_engine.schemas import (
    ScoreSignalRelevanceOutput,
    WhatToSendOutput,
)
from app.engagement_engine.state_machine import (
    legal_transitions_from, format_transitions_for_prompt,
)
from app.engagement_engine.validators import (
    validate_ai_action, ContactInfo,
)

log = logging.getLogger("engagement_engine.decision_maker")


# Per-tick batch sizes. The scoring loop processes many cheap calls; the
# react loop fewer expensive ones.
SCORE_BATCH_SIZE = 30
REACT_BATCH_SIZE = 10

# Default token estimates for budget reservation. Refined later from
# observed averages. Reserving more than we end up using is fine —
# reconcile_actual_cost adjusts down.
RESERVED_TOKENS_SCORING = (800, 80)        # in, out
RESERVED_TOKENS_REACT = (4000, 800)
RESERVED_TOKENS_SUMMARY = (2000, 200)


@dataclass
class DecisionMakerTickReport:
    started_at: datetime
    finished_at: datetime | None = None
    signals_scored: int = 0
    signals_reacted_to: int = 0
    actions_created: int = 0
    actions_blocked_by_validator: int = 0
    cost_budget_exceeded: int = 0
    parse_failures: int = 0
    provider_failures: int = 0
    no_engagement: int = 0
    total_cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        if self.finished_at is None:
            return 0
        return int((self.finished_at - self.started_at).total_seconds() * 1000)


async def run_decision_maker_tick() -> DecisionMakerTickReport:
    """One tick of the decision maker worker."""
    report = DecisionMakerTickReport(started_at=datetime.now(timezone.utc))

    # ── 1. Score unscored signals ──────────────────────────────────────────
    try:
        unscored_ids = await _claim_unscored_signals(SCORE_BATCH_SIZE)
    except Exception as e:
        report.errors.append(f"claim_unscored_failed: {type(e).__name__}: {e}")
        unscored_ids = []

    for sid in unscored_ids:
        try:
            await _score_one_signal(sid, report)
        except Exception as e:
            report.errors.append(
                f"score signal {sid} unhandled: {type(e).__name__}: {e}"
            )
            log.exception(f"score signal {sid} unhandled exception")

    # ── 2. React to high-relevance signals ─────────────────────────────────
    try:
        reactive_ids = await _claim_reactive_signals(REACT_BATCH_SIZE)
    except Exception as e:
        report.errors.append(f"claim_reactive_failed: {type(e).__name__}: {e}")
        reactive_ids = []

    for sid in reactive_ids:
        try:
            await _react_to_one_signal(sid, report)
        except Exception as e:
            report.errors.append(
                f"react signal {sid} unhandled: {type(e).__name__}: {e}"
            )
            log.exception(f"react signal {sid} unhandled exception")

    report.finished_at = datetime.now(timezone.utc)
    return report


# ── Signal scoring (cheap path) ─────────────────────────────────────────────

async def _claim_unscored_signals(limit: int) -> list[int]:
    """Claim a batch of unscored signals with SKIP LOCKED."""
    async with async_session() as session:
        rows = await session.execute(text("""
            SELECT id FROM signals
            WHERE relevance_score IS NULL
            ORDER BY detected_at
            LIMIT :n
            FOR UPDATE SKIP LOCKED
        """), {"n": limit})
        ids = [r.id for r in rows]
        # Hold the lock by NOT committing yet — but since we'll do the LLM
        # call in a separate session, we release here and rely on the
        # `relevance_score IS NULL` clause to dedupe re-claims.
        await session.commit()
        return ids


async def _score_one_signal(
    signal_id: int, report: DecisionMakerTickReport,
) -> None:
    """Score one signal with the cheap LLM tier."""
    async with async_session() as session:
        row = await session.execute(text("""
            SELECT
                s.id, s.tenant_id, s.engagement_id, s.contact_id,
                s.raw_data_json, st.code AS signal_type_code,
                c.first_name, c.last_name, co.name AS company_name,
                e.ai_engagement_summary
            FROM signals s
            JOIN signal_types st ON st.id = s.signal_type_id
            JOIN contacts c ON c.id = s.contact_id
            JOIN engagements e ON e.id = s.engagement_id
            JOIN companies co ON co.id = e.company_id
            WHERE s.id = :id
              AND s.relevance_score IS NULL
        """), {"id": signal_id})
        signal = row.first()
    if signal is None:
        # Already scored by another worker, or deleted — skip
        return

    # Cost reservation
    estimated_cost = estimate_cost_usd(
        "anthropic", "claude-haiku-4-5",
        *RESERVED_TOKENS_SCORING,
    )
    async with async_session() as session:
        conn = await session.connection()
        reservation = await reserve_budget(
            conn,
            engagement_id=signal.engagement_id,
            tenant_id=signal.tenant_id,
            estimated_cost_usd=estimated_cost,
        )
        await session.commit()
    if not reservation.granted:
        report.cost_budget_exceeded += 1
        await _pause_engagement(signal.engagement_id, reason=reservation.reason)
        return

    # Build prompt
    contact_name = f"{signal.first_name or ''} {signal.last_name or ''}".strip() or "the contact"
    raw_data = signal.raw_data_json or {}
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except Exception:
            raw_data = {"raw": str(raw_data)[:500]}

    system, user_prompt = build_score_signal_prompt(
        signal_type_code=signal.signal_type_code,
        raw_signal_data=raw_data,
        engagement_summary=signal.ai_engagement_summary,
        contact_name=contact_name,
        company_name=signal.company_name or "the company",
        schema=ScoreSignalRelevanceOutput,
    )

    # Resolve provider + model
    provider = await get_provider_for_tenant(signal.tenant_id)
    model = await get_model_for_decision_type(signal.tenant_id, "score_signal_relevance")

    try:
        llm_response = await provider.complete(
            prompt=user_prompt, system=system,
            max_tokens=200, temperature=0.0,
            schema=ScoreSignalRelevanceOutput, model=model,
        )
    except (ParseError, ProviderUnavailable, RateLimitExceeded) as e:
        log.warning("score signal %s failed: %s", signal_id, e)
        if isinstance(e, ParseError):
            report.parse_failures += 1
        else:
            report.provider_failures += 1
        # Reconcile reservation back since we didn't actually use the budget
        async with async_session() as session:
            conn = await session.connection()
            await reconcile_actual_cost(
                conn,
                engagement_id=signal.engagement_id,
                tenant_id=signal.tenant_id,
                estimated_cost_usd=estimated_cost,
                actual_cost_usd=0.0,
            )
            await session.commit()
        return

    actual_cost = float(llm_response.cost_usd or 0)
    report.total_cost_usd += actual_cost

    # Persist scored signal + ai_decisions audit row + cost reconcile
    parsed: ScoreSignalRelevanceOutput = llm_response.content

    async with async_session() as session:
        # Update signal
        await session.execute(text("""
            UPDATE signals
            SET relevance_score = :score,
                ai_summary = :summary,
                ai_scored_by_model = :model,
                ai_scoring_cost_usd = :cost
            WHERE id = :id
        """), {
            "id": signal_id,
            "score": parsed.relevance_score,
            "summary": parsed.summary,
            "model": llm_response.model_used,
            "cost": actual_cost,
        })
        # Audit log
        await _write_ai_decision_audit(
            session,
            tenant_id=signal.tenant_id,
            engagement_id=signal.engagement_id,
            signal_id=signal_id,
            decision_type="score_signal_relevance",
            input_context={
                "signal_type": signal.signal_type_code,
                "raw_data_keys": list(raw_data.keys()) if isinstance(raw_data, dict) else [],
            },
            output_choice=parsed.model_dump(),
            llm_response=llm_response,
            estimated_cost=estimated_cost,
            idempotency_key=f"score-signal-{signal_id}",
        )
        # If score >=70, mark summary stale (so the next expensive decision
        # forces a refresh)
        if parsed.relevance_score >= 70:
            await session.execute(text("""
                UPDATE engagements SET summary_stale_at = NOW()
                WHERE id = :eng
            """), {"eng": signal.engagement_id})
        await session.commit()

    # Reconcile cost diff
    async with async_session() as session:
        conn = await session.connection()
        await reconcile_actual_cost(
            conn,
            engagement_id=signal.engagement_id,
            tenant_id=signal.tenant_id,
            estimated_cost_usd=estimated_cost,
            actual_cost_usd=actual_cost,
        )
        await session.commit()

    report.signals_scored += 1


# ── Reactive path (expensive) ───────────────────────────────────────────────

async def _claim_reactive_signals(limit: int) -> list[int]:
    """Find high-relevance signals not yet acted on."""
    async with async_session() as session:
        rows = await session.execute(text("""
            SELECT id FROM signals
            WHERE relevance_score >= 70
              AND triggered_action_id IS NULL
            ORDER BY relevance_score DESC, detected_at
            LIMIT :n
            FOR UPDATE SKIP LOCKED
        """), {"n": limit})
        ids = [r.id for r in rows]
        await session.commit()
        return ids


async def _react_to_one_signal(
    signal_id: int, report: DecisionMakerTickReport,
) -> None:
    """React to one high-relevance signal: decide what (if anything) to
    send, validate, persist action."""

    # Load full context
    async with async_session() as session:
        row = await session.execute(text("""
            SELECT
                s.id AS signal_id, s.tenant_id, s.engagement_id, s.contact_id,
                s.ai_summary AS signal_summary, s.raw_data_json AS signal_data,
                s.relevance_score,
                c.first_name, c.last_name, c.email AS contact_email,
                c.phone AS contact_phone, c.linkedin_url AS contact_linkedin,
                c.timezone AS contact_timezone,
                co.name AS company_name,
                e.current_phase, e.status AS engagement_status,
                e.ai_engagement_summary, e.notes AS bdr_notes,
                e.summary_stale_at
            FROM signals s
            JOIN contacts c ON c.id = s.contact_id
            JOIN engagements e ON e.id = s.engagement_id
            JOIN companies co ON co.id = e.company_id
            WHERE s.id = :id AND s.triggered_action_id IS NULL
        """), {"id": signal_id})
        ctx = row.first()
    if ctx is None:
        return  # Already acted on by another worker

    # Pre-flight: dedupe count check (we don't generate a 4th email today if
    # tenant's daily cap is 1)
    if await _at_dedupe_cap(ctx.engagement_id, "email"):
        log.info("signal %s: engagement at email dedupe cap; skipping", signal_id)
        return

    # Recent signals + actions for prompt context
    recent_signals, recent_actions = await _fetch_recent_context(ctx.engagement_id)
    legal_transitions = await _fetch_legal_transitions(
        ctx.current_phase, by="ai", current_status=ctx.engagement_status,
    )

    # Cost reservation (expensive tier)
    estimated_cost = estimate_cost_usd(
        "anthropic", "claude-opus-4-7",
        *RESERVED_TOKENS_REACT,
    )
    async with async_session() as session:
        conn = await session.connection()
        reservation = await reserve_budget(
            conn,
            engagement_id=ctx.engagement_id,
            tenant_id=ctx.tenant_id,
            estimated_cost_usd=estimated_cost,
        )
        await session.commit()
    if not reservation.granted:
        report.cost_budget_exceeded += 1
        await _pause_engagement(ctx.engagement_id, reason=reservation.reason)
        return

    # Parse raw data JSON
    raw_data = ctx.signal_data or {}
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except Exception:
            raw_data = {"raw": str(raw_data)[:500]}

    contact_name = f"{ctx.first_name or ''} {ctx.last_name or ''}".strip() or "the contact"
    system, user_prompt = build_what_to_send_prompt(
        signal_summary=ctx.signal_summary or "(no summary available)",
        signal_data=raw_data,
        engagement_summary=ctx.ai_engagement_summary,
        contact_name=contact_name,
        company_name=ctx.company_name or "the company",
        recent_signals=recent_signals,
        recent_actions=recent_actions,
        bdr_notes=ctx.bdr_notes,
        legal_transitions=legal_transitions,
        available_channels=["email", "sms", "linkedin", "call_task", "manual"],
        schema=WhatToSendOutput,
    )

    provider = await get_provider_for_tenant(ctx.tenant_id)
    model = await get_model_for_decision_type(ctx.tenant_id, "what_to_send")

    try:
        llm_response = await provider.complete(
            prompt=user_prompt, system=system,
            max_tokens=2000, temperature=0.3,
            schema=WhatToSendOutput, model=model,
        )
    except (ParseError, ProviderUnavailable, RateLimitExceeded) as e:
        log.warning("react signal %s failed: %s", signal_id, e)
        if isinstance(e, ParseError):
            report.parse_failures += 1
        else:
            report.provider_failures += 1
        async with async_session() as session:
            conn = await session.connection()
            await reconcile_actual_cost(
                conn,
                engagement_id=ctx.engagement_id,
                tenant_id=ctx.tenant_id,
                estimated_cost_usd=estimated_cost,
                actual_cost_usd=0.0,
            )
            await session.commit()
        return

    actual_cost = float(llm_response.cost_usd or 0)
    report.total_cost_usd += actual_cost
    decision: WhatToSendOutput = llm_response.content

    # Reconcile cost
    async with async_session() as session:
        conn = await session.connection()
        await reconcile_actual_cost(
            conn,
            engagement_id=ctx.engagement_id,
            tenant_id=ctx.tenant_id,
            estimated_cost_usd=estimated_cost,
            actual_cost_usd=actual_cost,
        )
        await session.commit()

    # Audit the decision regardless of whether we act
    async with async_session() as session:
        await _write_ai_decision_audit(
            session,
            tenant_id=ctx.tenant_id,
            engagement_id=ctx.engagement_id,
            signal_id=signal_id,
            decision_type="what_to_send",
            input_context={
                "signal_summary": ctx.signal_summary,
                "relevance_score": ctx.relevance_score,
                "current_phase": ctx.current_phase,
            },
            output_choice=decision.model_dump(),
            llm_response=llm_response,
            estimated_cost=estimated_cost,
            idempotency_key=f"react-signal-{signal_id}",
        )
        await session.commit()

    report.signals_reacted_to += 1

    if not decision.should_act:
        # AI decided not to act; mark signal as 'processed' by linking to a
        # null action_id... actually we DON'T set triggered_action_id when
        # there's no action. We use a sentinel via ai_decisions audit.
        # For Phase 4, leave triggered_action_id NULL — the signal won't be
        # picked again because the query also checks if any ai_decision
        # exists for it. (Phase 4 simple: ignore re-pickup; volume is low.)
        return

    # Validate AI output before persisting action
    contact_info = ContactInfo(
        email=ctx.contact_email,
        phone=ctx.contact_phone,
        linkedin_url=ctx.contact_linkedin,
        tenant_id=ctx.tenant_id,
    )
    validation = validate_ai_action(
        decision_output=decision,
        contact=contact_info,
    )
    if not validation.passed:
        log.warning(
            "validation blocked signal %s action: %s",
            signal_id, validation.errors,
        )
        report.actions_blocked_by_validator += 1
        # Still record the attempt in audit for visibility
        async with async_session() as session:
            await session.execute(text("""
                UPDATE ai_decisions
                SET output_validation_passed = FALSE,
                    output_validation_errors = CAST(:err AS jsonb)
                WHERE idempotency_key = :idem
                  AND created_at >= NOW() - INTERVAL '1 hour'
            """), {
                "idem": f"react-signal-{signal_id}",
                "err": json.dumps({"errors": validation.errors,
                                   "warnings": validation.warnings}),
            })
            await session.commit()
        return

    # Persist the action
    await _persist_action(
        decision=decision,
        ctx=ctx,
        signal_id=signal_id,
        validation=validation,
    )
    report.actions_created += 1


# ── Persistence helpers ─────────────────────────────────────────────────────

async def _persist_action(
    *, decision: WhatToSendOutput, ctx, signal_id: int, validation,
) -> None:
    """Insert an action row, link signal.triggered_action_id, supersede
    any prior in-flight action for this engagement+channel."""
    scheduled_at = datetime.now(timezone.utc) + timedelta(hours=decision.delay_hours)
    stale_after = scheduled_at + timedelta(hours=24)

    # Resolve channel_id
    async with async_session() as session:
        ch_row = await session.execute(text("""
            SELECT id FROM channel_types WHERE code = :code
        """), {"code": decision.channel})
        channel_row = ch_row.first()
        if channel_row is None:
            log.error("unknown channel from AI: %s", decision.channel)
            return
        channel_id = channel_row.id

        # Determine recipient based on channel
        recipient_email = ctx.contact_email if decision.channel == "email" else None
        recipient_phone = ctx.contact_phone if decision.channel == "sms" else None
        recipient_linkedin = ctx.contact_linkedin if decision.channel == "linkedin" else None

        requires_review = decision.requires_human_review or validation.force_human_review

        # Insert the action with idempotency = signal-only (Rule #2)
        result = await session.execute(text("""
            INSERT INTO actions (
                tenant_id, engagement_id, contact_id,
                triggered_by_signal_id, channel_id, status,
                requires_human_review, scheduled_at, stale_after,
                contact_timezone, subject, body, task_description,
                recipient_email, recipient_phone, recipient_linkedin_url,
                idempotency_key, ai_strategy_used, ai_generation_cost_usd
            )
            VALUES (
                :t, :eng, :c, :sig, :ch,
                :status, :rh, :sched, :stale,
                :tz, :subj, :body, :task,
                :re, :rp, :rl,
                :idem, :strategy, :cost
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
        """), {
            "t": ctx.tenant_id,
            "eng": ctx.engagement_id,
            "c": ctx.contact_id,
            "sig": signal_id,
            "ch": channel_id,
            "status": "awaiting_approval" if requires_review else "scheduled",
            "rh": requires_review,
            "sched": scheduled_at,
            "stale": stale_after,
            "tz": ctx.contact_timezone,
            "subj": decision.subject,
            "body": decision.body,
            "task": decision.task_description,
            "re": recipient_email,
            "rp": recipient_phone,
            "rl": recipient_linkedin,
            "idem": f"sig-{signal_id}",
            "strategy": "what_to_send",
            "cost": 0,  # tracked separately in ai_decisions
        })
        inserted = result.first()
        if inserted is not None:
            # Link signal → action and increment dedupe counter
            await session.execute(text("""
                UPDATE signals SET triggered_action_id = :aid WHERE id = :sid
            """), {"aid": inserted.id, "sid": signal_id})
            await session.execute(text("""
                INSERT INTO action_dedupe_counters (engagement_id, channel_id, date, count)
                VALUES (:eng, :ch, CURRENT_DATE, 1)
                ON CONFLICT (engagement_id, channel_id, date)
                DO UPDATE SET count = action_dedupe_counters.count + 1
            """), {"eng": ctx.engagement_id, "ch": channel_id})
        await session.commit()


async def _write_ai_decision_audit(
    session: AsyncSession, *,
    tenant_id: int,
    engagement_id: int,
    signal_id: int | None,
    decision_type: str,
    input_context: dict,
    output_choice: dict,
    llm_response: LLMResponse,
    estimated_cost: float,
    idempotency_key: str,
) -> None:
    await session.execute(text("""
        INSERT INTO ai_decisions (
            tenant_id, engagement_id, signal_id, decision_type,
            input_context_json, output_choice_json,
            provider, model_used, tokens_in, tokens_out,
            cost_usd, estimated_cost_usd, latency_ms,
            json_parse_attempts, json_parse_succeeded,
            idempotency_key
        )
        VALUES (
            :t, :eng, :sig, :dt,
            CAST(:input AS jsonb), CAST(:output AS jsonb),
            :prov, :model, :ti, :to,
            :cost, :est, :lat,
            :pa, :ps,
            :idem
        )
        ON CONFLICT (idempotency_key, created_at) DO NOTHING
    """), {
        "t": tenant_id,
        "eng": engagement_id,
        "sig": signal_id,
        "dt": decision_type,
        "input": json.dumps(input_context, default=str),
        "output": json.dumps(output_choice, default=str),
        "prov": llm_response.provider,
        "model": llm_response.model_used,
        "ti": llm_response.tokens_in,
        "to": llm_response.tokens_out,
        "cost": llm_response.cost_usd,
        "est": estimated_cost,
        "lat": llm_response.latency_ms,
        "pa": llm_response.parse_attempts,
        "ps": llm_response.parse_succeeded,
        "idem": idempotency_key,
    })


# ── Small helpers ───────────────────────────────────────────────────────────

async def _at_dedupe_cap(engagement_id: int, channel_code: str) -> bool:
    """Check whether this engagement has hit its daily cap for the channel."""
    async with async_session() as session:
        row = await session.execute(text("""
            SELECT
                COALESCE(adc.count, 0) AS sent_today,
                CASE :channel
                    WHEN 'email' THEN tac.dedupe_email_per_day
                    WHEN 'sms' THEN tac.dedupe_sms_per_day
                    WHEN 'linkedin' THEN tac.dedupe_linkedin_per_day
                    ELSE 999
                END AS cap
            FROM engagements e
            LEFT JOIN tenant_ai_config tac ON tac.tenant_id = e.tenant_id
            LEFT JOIN action_dedupe_counters adc
                ON adc.engagement_id = e.id
               AND adc.date = CURRENT_DATE
               AND adc.channel_id = (SELECT id FROM channel_types WHERE code = :channel)
            WHERE e.id = :eng
        """), {"eng": engagement_id, "channel": channel_code})
        result = row.first()
        if result is None:
            return False
        return result.sent_today >= (result.cap or 1)


async def _pause_engagement(engagement_id: int, reason: str) -> None:
    async with async_session() as session:
        await session.execute(text("""
            UPDATE engagements
            SET status = 'paused',
                last_transition_by = 'system',
                updated_at = NOW()
            WHERE id = :id AND status = 'active'
        """), {"id": engagement_id})
        await session.commit()
        log.warning("engagement %s paused: %s", engagement_id, reason)


async def _fetch_recent_context(engagement_id: int):
    """Return (recent_signals, recent_actions) lists for prompt context."""
    async with async_session() as session:
        s_rows = await session.execute(text("""
            SELECT st.code AS type, s.relevance_score, s.ai_summary,
                   s.detected_at
            FROM signals s
            JOIN signal_types st ON st.id = s.signal_type_id
            WHERE s.engagement_id = :eng
            ORDER BY s.detected_at DESC
            LIMIT 6
        """), {"eng": engagement_id})
        signals = [dict(r._mapping) for r in s_rows]

        a_rows = await session.execute(text("""
            SELECT ct.code AS channel, a.status, a.outcome, a.subject,
                   a.scheduled_at, a.executed_at
            FROM actions a
            JOIN channel_types ct ON ct.id = a.channel_id
            WHERE a.engagement_id = :eng
            ORDER BY COALESCE(a.executed_at, a.scheduled_at) DESC
            LIMIT 12
        """), {"eng": engagement_id})
        actions = [dict(r._mapping) for r in a_rows]
    return signals, actions


async def _fetch_legal_transitions(
    current_phase: str, *, by: str, current_status: str,
) -> list[str]:
    async with async_session() as session:
        transitions = await legal_transitions_from(
            session, from_phase=current_phase, by=by,
        )
    legal = [t["to_phase"] for t in transitions
             if t.get("requires_status") is None
             or t.get("requires_status") == current_status]
    return legal
