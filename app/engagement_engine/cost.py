"""Atomic cost reservation for LLM calls.

Rule #10 of the design: cost budget enforced atomically via UPDATE-WHERE.
The naive pattern (read cost, call LLM, write cost) opens a window where
many concurrent calls all see the stale low number and blow the budget.

Pattern:
    UPDATE engagements
    SET monthly_ai_cost_usd = monthly_ai_cost_usd + :estimated
    WHERE id = :eng_id
      AND monthly_ai_cost_usd + :estimated <= :cap
      AND status = 'active'
    RETURNING monthly_ai_cost_usd

Zero rows returned → budget exhausted OR engagement not active → block.
Row returned → budget reserved, LLM call may proceed.

After LLM call, reconcile actual cost:
    UPDATE engagements
    SET monthly_ai_cost_usd = monthly_ai_cost_usd - :estimated + :actual
    WHERE id = :eng_id

Same pattern applied at tenant level via tenant_ai_config.

Static price fallback: when provider doesn't report token usage (Ollama,
vLLM, Bedrock errors), estimate cost from a static price table keyed by
(provider, model).
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


# Static fallback prices ($ per million tokens, input / output) keyed by
# (provider, model). Updated when providers publish new pricing.
# Used when the live response doesn't include usage data (Ollama, vLLM,
# some error paths, etc).
STATIC_PRICE_TABLE_USD_PER_M_TOKENS: dict[tuple[str, str], tuple[float, float]] = {
    # Anthropic — current Claude Opus 4.7 + 4.8 family
    ("anthropic", "claude-opus-4-8"):       (15.0,  75.0),
    ("anthropic", "claude-opus-4-7"):       (15.0,  75.0),
    ("anthropic", "claude-sonnet-4-6"):     ( 3.0,  15.0),
    ("anthropic", "claude-haiku-4-5"):      ( 1.0,   5.0),
    # OpenAI — frontier models
    ("openai",    "gpt-5"):                 (10.0,  30.0),
    ("openai",    "gpt-4o"):                ( 2.5,  10.0),
    ("openai",    "gpt-4o-mini"):           ( 0.15,  0.60),
    # Google Gemini
    ("google_gemini", "gemini-2-pro"):      ( 7.0,  21.0),
    ("google_gemini", "gemini-2-flash"):    ( 0.30,  1.20),
    # OpenRouter — variable; we treat OpenRouter as pass-through and rely
    # on it reporting cost in the response. These are fallbacks.
    ("openrouter", "deepseek/deepseek-v3"): ( 0.27,  1.10),
    ("openrouter", "meta-llama/llama-3.3"): ( 0.50,  1.50),
    # Self-hosted (no real cost beyond compute) — used to keep accounting
    # consistent at near-zero.
    ("ollama",    "*"):                     ( 0.01,  0.01),
    ("vllm",      "*"):                     ( 0.01,  0.01),
    # AAMP default (we proxy through Anthropic; bill tenant at a markup)
    ("aamp_default", "claude-opus-4-7"):    (15.0,  75.0),
    ("aamp_default", "claude-sonnet-4-6"):  ( 3.0,  15.0),
    ("aamp_default", "claude-haiku-4-5"):   ( 1.0,   5.0),
}


@dataclass
class CostReservation:
    """Outcome of attempting to reserve cost budget for an LLM call."""
    granted: bool
    reason: str | None = None
    new_engagement_total: Decimal | None = None  # only when granted=True
    new_tenant_total: Decimal | None = None


def estimate_cost_usd(
    provider: str, model: str, tokens_in: int, tokens_out: int,
) -> float:
    """Estimate cost from token counts + static price table.

    Used pre-call (to know how much to reserve) and as a fallback when
    the provider's response lacks usage data.
    """
    key = (provider, model)
    if key not in STATIC_PRICE_TABLE_USD_PER_M_TOKENS:
        # Try the provider wildcard ('*' model)
        key = (provider, "*")
    if key not in STATIC_PRICE_TABLE_USD_PER_M_TOKENS:
        # Final fallback — moderate Sonnet-like pricing so we never under-account
        in_per_m, out_per_m = (3.0, 15.0)
    else:
        in_per_m, out_per_m = STATIC_PRICE_TABLE_USD_PER_M_TOKENS[key]
    in_cost = (tokens_in / 1_000_000) * in_per_m
    out_cost = (tokens_out / 1_000_000) * out_per_m
    return round(in_cost + out_cost, 5)


async def reserve_budget(
    conn: AsyncConnection,
    *,
    engagement_id: int,
    tenant_id: int,
    estimated_cost_usd: float,
) -> CostReservation:
    """Atomically reserve budget at both engagement and tenant level.

    Returns granted=True iff BOTH:
      1. engagement.monthly_ai_cost_usd + estimated <= per_engagement_budget_usd
         AND engagement.status = 'active'
      2. tenant_ai_config.current_month_spent_usd + estimated <= monthly_budget_usd
         (or monthly_budget_usd IS NULL — no tenant-level cap)

    If engagement reservation succeeds but tenant fails, the engagement
    reservation is rolled back (subtract estimated) so we don't leak cost
    accounting.
    """
    # 1) Reserve at engagement level (with cap lookup)
    row = await conn.execute(text("""
        UPDATE engagements e
        SET monthly_ai_cost_usd = monthly_ai_cost_usd + :est
        FROM tenant_ai_config tac
        WHERE e.id = :eid
          AND tac.tenant_id = e.tenant_id
          AND e.status = 'active'
          AND (e.monthly_ai_cost_usd + :est) <= tac.per_engagement_budget_usd
        RETURNING e.monthly_ai_cost_usd
    """), {"eid": engagement_id, "est": estimated_cost_usd})
    eng_row = row.first()
    if eng_row is None:
        return CostReservation(
            granted=False,
            reason="engagement_budget_exceeded_or_inactive",
        )

    # 2) Reserve at tenant level (only if a monthly_budget_usd cap is set)
    row = await conn.execute(text("""
        UPDATE tenant_ai_config
        SET current_month_spent_usd = current_month_spent_usd + :est
        WHERE tenant_id = :tid
          AND (monthly_budget_usd IS NULL
               OR (current_month_spent_usd + :est) <= monthly_budget_usd)
        RETURNING current_month_spent_usd
    """), {"tid": tenant_id, "est": estimated_cost_usd})
    tenant_row = row.first()
    if tenant_row is None:
        # Roll back engagement-level reservation
        await conn.execute(text("""
            UPDATE engagements
            SET monthly_ai_cost_usd = monthly_ai_cost_usd - :est
            WHERE id = :eid
        """), {"eid": engagement_id, "est": estimated_cost_usd})
        return CostReservation(
            granted=False,
            reason="tenant_budget_exceeded",
        )

    return CostReservation(
        granted=True,
        new_engagement_total=eng_row.monthly_ai_cost_usd,
        new_tenant_total=tenant_row.current_month_spent_usd,
    )


async def reconcile_actual_cost(
    conn: AsyncConnection,
    *,
    engagement_id: int,
    tenant_id: int,
    estimated_cost_usd: float,
    actual_cost_usd: float,
) -> None:
    """After the LLM call returns actual cost, adjust the reservations.

    diff = actual - estimated. May be positive (slight over-run, still
    within cap — accept it) or negative (we reserved more than needed).
    """
    diff = round(actual_cost_usd - estimated_cost_usd, 5)
    if abs(diff) < 1e-6:
        return  # no adjustment needed

    await conn.execute(text("""
        UPDATE engagements
        SET monthly_ai_cost_usd = monthly_ai_cost_usd + :diff
        WHERE id = :eid
    """), {"eid": engagement_id, "diff": diff})
    await conn.execute(text("""
        UPDATE tenant_ai_config
        SET current_month_spent_usd = current_month_spent_usd + :diff
        WHERE tenant_id = :tid
    """), {"tid": tenant_id, "diff": diff})


async def reset_monthly_budgets_if_due(conn: AsyncConnection) -> int:
    """Reset monthly_ai_cost_usd on engagements + current_month_spent_usd on
    tenants if the month rolled over since last reset.

    Idempotent: safe to run on every dispatcher tick (or hourly cron).
    Returns the number of rows reset across the two tables (rough metric).
    """
    eng_result = await conn.execute(text("""
        UPDATE engagements
        SET monthly_ai_cost_usd = 0,
            monthly_ai_cost_reset_at = date_trunc('month', NOW())
        WHERE monthly_ai_cost_reset_at < date_trunc('month', NOW())
    """))
    tenant_result = await conn.execute(text("""
        UPDATE tenant_ai_config
        SET current_month_spent_usd = 0,
            current_month_reset_at = date_trunc('month', NOW())
        WHERE current_month_reset_at < date_trunc('month', NOW())
    """))
    return (eng_result.rowcount or 0) + (tenant_result.rowcount or 0)
