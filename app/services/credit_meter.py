"""
Credit meter — per-action billing ledger.

Two layers:
  1. credits_debited — customer-facing units (1 credit ≈ $0.005 cost)
  2. raw_cost_usd     — what we actually pay vendors (admin COGS view)

Shim mode (current): every billable call site emits a meter() row, but
nothing enforces a balance. We collect cost data for 1-2 weeks, then turn
on enforcement once the rate card is grounded in real usage.

Idempotency: every meter() call MUST pass an idempotency_key derived from
the underlying action's natural ID (e.g. "email_send:{generated_email_id}").
Re-fires of the same key are silent no-ops, so webhook retries / re-runs
of the sequence engine don't double-charge.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Any
import json
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import CreditLedger

log = logging.getLogger("bmp.credit_meter")


@dataclass(frozen=True)
class ActionRate:
    credits: int        # what we charge tenants
    raw_cost_usd: float # what we pay the vendor
    vendor: str


# Rate card. Edit in code for now; admin UI override comes later.
# Margin column: credits * 0.005 / raw_cost_usd. Aim ~50% (1.5x markup).
RATE_CARD: dict[str, ActionRate] = {
    # Email
    "email_send":         ActionRate(credits=1,  raw_cost_usd=0.0004, vendor="resend"),
    "email_verify":       ActionRate(credits=8,  raw_cost_usd=0.04,   vendor="hunter"),
    # AI
    "ai_email_gen":       ActionRate(credits=2,  raw_cost_usd=0.005,  vendor="anthropic"),
    "ai_reply_classify":  ActionRate(credits=1,  raw_cost_usd=0.001,  vendor="anthropic"),
    "ai_chat_turn":       ActionRate(credits=5,  raw_cost_usd=0.015,  vendor="anthropic"),
    "ai_summary":         ActionRate(credits=2,  raw_cost_usd=0.005,  vendor="anthropic"),
    # Enrichment — paid providers
    "enrich_netrows":     ActionRate(credits=10, raw_cost_usd=0.055,  vendor="netrows"),
    "enrich_hunter":      ActionRate(credits=8,  raw_cost_usd=0.04,   vendor="hunter"),
    # Apollo BYO-key — tenant pays Apollo directly, we charge a smaller orchestration fee.
    "enrich_apollo":      ActionRate(credits=2,  raw_cost_usd=0.0,    vendor="apollo_byo"),
    # Phone
    "phone_lookup":       ActionRate(credits=1,  raw_cost_usd=0.005,  vendor="twilio"),
    "sms_send":           ActionRate(credits=2,  raw_cost_usd=0.008,  vendor="twilio"),
    "voice_minute":       ActionRate(credits=20, raw_cost_usd=0.10,   vendor="twilio"),
    # Scraping / compute (small marginal cost — server time)
    "scrape_yelp":        ActionRate(credits=1,  raw_cost_usd=0.001,  vendor="internal"),
    "scrape_maps":        ActionRate(credits=1,  raw_cost_usd=0.001,  vendor="internal"),
}


def get_rate(action_type: str) -> ActionRate:
    """Look up the rate card. Unknown actions get a 0/0 rate so the
    meter still records the row but no charge happens."""
    return RATE_CARD.get(action_type, ActionRate(credits=0, raw_cost_usd=0.0, vendor="unknown"))


async def meter(
    db: AsyncSession,
    *,
    action_type: str,
    idempotency_key: str,
    user_id: Optional[int] = None,
    action_ref: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    raw_cost_override_usd: Optional[float] = None,
    units: float = 1.0,
    tenant_id: Optional[int] = None,
) -> Optional[CreditLedger]:
    """Record a billable action.

    Returns the CreditLedger row that was inserted (or None if this
    idempotency_key already had a row — silent dedupe).

    `units` lets one call charge for multiple units (e.g., 3 email_sends
    in a single sequence batch). Defaults to 1.

    `raw_cost_override_usd` is for actions where the vendor charges
    variably and we want the actual amount (e.g. voice_minute × 2.4 min).
    Falls back to rate.raw_cost_usd when not provided.

    NEVER raises. Meter failures must not break the underlying action —
    we'd rather lose a billing row than fail a customer's email send.
    """
    try:
        rate = get_rate(action_type)
        credits = int(round(rate.credits * units))
        if raw_cost_override_usd is not None:
            raw_cost = float(raw_cost_override_usd)
        else:
            raw_cost = float(rate.raw_cost_usd * units)

        # Idempotency check — most call sites only retry on transient errors,
        # so the upsert path is rare. We do a quick existence check first
        # to avoid an unnecessary INSERT on the unique-index conflict path.
        existing = (await db.execute(
            select(CreditLedger).where(CreditLedger.idempotency_key == idempotency_key)
        )).scalar_one_or_none()
        if existing is not None:
            return None

        meta_json = json.dumps(metadata, default=str) if metadata else None
        row = CreditLedger(
            user_id=user_id,
            action_type=action_type,
            action_ref=action_ref,
            credits_debited=credits,
            raw_cost_usd=raw_cost,
            vendor=rate.vendor,
            idempotency_key=idempotency_key,
            metadata_json=meta_json,
        )
        # Callers outside a tenant-scoped session (the engagement-engine
        # dispatcher) pass tenant_id explicitly so the spend books to the
        # right tenant instead of the TenantMixin default (tenant #1).
        if tenant_id is not None:
            row.tenant_id = tenant_id
        db.add(row)
        await db.flush()
        return row
    except Exception as e:
        log.warning(f"credit_meter.meter failed (action={action_type} key={idempotency_key}): {e}")
        return None


def make_idem_key(action_type: str, *parts: Any) -> str:
    """Build a stable idempotency key from the action + entity parts.

    Example: make_idem_key("email_send", 1234) -> "email_send:1234"
             make_idem_key("ai_email_gen", company_id, contact_id) -> "ai_email_gen:567:89"
    """
    return f"{action_type}:" + ":".join(str(p) for p in parts if p is not None)


async def meter_standalone(
    *,
    action_type: str,
    idempotency_key: Optional[str] = None,
    user_id: Optional[int] = None,
    action_ref: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    raw_cost_override_usd: Optional[float] = None,
    units: float = 1.0,
    tenant_id: Optional[int] = None,
) -> None:
    """Same as meter() but opens its own DB session.

    Use when the call site has no db session in scope — e.g. AI gen
    helpers that are called from many places (sequence engine, routes,
    background tasks). Commits independently; does not interfere with
    the caller's transaction.

    Idempotency key defaults to a random per-call token, since AI gen
    calls are intrinsically unique (each invocation IS a new spend).
    """
    if idempotency_key is None:
        import secrets as _s
        idempotency_key = make_idem_key(action_type, _s.token_hex(8))
    try:
        from app.database import async_session
        async with async_session() as db:
            await meter(
                db,
                action_type=action_type,
                idempotency_key=idempotency_key,
                user_id=user_id,
                action_ref=action_ref,
                metadata=metadata,
                raw_cost_override_usd=raw_cost_override_usd,
                units=units,
                tenant_id=tenant_id,
            )
            await db.commit()
    except Exception as e:
        # Same fail-open contract as meter(): never break the caller.
        log.warning(f"credit_meter.meter_standalone failed (action={action_type}): {e}")
