"""Protocol interfaces for engagement engine pluggable components.

Three Protocol classes that define the contracts pluggable adapters must
fulfill:

  - LLMProvider:   BYO AI per tenant (Rule #11). Adapters: Anthropic, OpenAI,
                   OpenRouter, Google Gemini, Ollama, etc. Strict JSON-schema
                   validation with retry + fallback_provider on parse failure.

  - ActionDispatcher: Per-channel send path. Adapters: Email, SMS, LinkedIn,
                     BDR call task, Manual. Each implements pre_dispatch_guards
                     + send + outcome_fetch.

  - SignalSource: Per-source polling adapter. Adapters: GMB, Website, Hiring,
                  LinkedIn (Phase 8), etc. Each implements fetch + extract_signals.

Concrete adapter implementations land in Phase 2 (channels), Phase 3 (sources),
Phase 4 (LLM providers). Phase 1 only defines the contracts.
"""
from __future__ import annotations
from typing import Protocol, runtime_checkable, Any
from dataclasses import dataclass


# ════════════════════════════════════════════════════════════════════════════
# Shared response/result types
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class LLMResponse:
    """Universal response shape from any LLM provider adapter.

    cost_usd may be 0 when the provider doesn't report token usage (e.g.,
    self-hosted Ollama). In that case the caller falls back to a static
    price table for budget accounting.
    """
    content: Any  # parsed Pydantic model when schema given, else raw str
    raw_content: str
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    model_used: str
    provider: str
    latency_ms: int
    parse_attempts: int = 1
    parse_succeeded: bool = True


@dataclass
class GuardResult:
    """Per-channel pre-dispatch check result. blocked=True halts the action
    with the given reason captured into actions.skip_reason."""
    blocked: bool
    reason: str | None = None


@dataclass
class SendResult:
    """Channel-agnostic dispatch outcome."""
    success: bool
    external_id: str | None = None  # resend_message_id / twilio_call_sid / etc.
    error_message: str | None = None
    cost_usd: float | None = None


@dataclass
class OutcomeUpdate:
    """Post-send outcome update (open, click, reply, delivery confirmation)."""
    outcome: str  # 'opened' | 'clicked' | 'replied' | 'delivered' | ...
    observed_at: Any  # datetime
    metadata: dict | None = None


@dataclass
class Snapshot:
    """A signal source's polled state at a point in time. content_hash is
    used by the signal_watcher to diff-detect and avoid invoking AI scoring
    when nothing changed."""
    content_hash: str
    raw_data: dict
    observed_at: Any  # datetime


@dataclass
class ExtractedSignal:
    """A signal extracted from comparing prev vs current source snapshots.
    Multiple signals can come from one snapshot diff (e.g., GMB poll yields
    new_review + new_post + listing_change)."""
    signal_type_code: str  # FK code into signal_types lookup
    extracted_facts: dict
    source_url: str | None = None


# ════════════════════════════════════════════════════════════════════════════
# Protocol interfaces
# ════════════════════════════════════════════════════════════════════════════

@runtime_checkable
class LLMProvider(Protocol):
    """BYO AI per tenant (Rule #11).

    Adapter responsibilities:
      1. Normalize rate-limit, downtime, parse-failure semantics.
      2. Compute cost_usd from response usage; fall back to static price table.
      3. Validate JSON against Pydantic schema before returning.
      4. Retry once on parse failure with corrective prompt.
      5. Raise ParseError after retry exhausted (caller may try fallback_provider).
      6. Raise RateLimitExceeded / ProviderUnavailable for upstream handling.
    """
    name: str  # 'anthropic', 'openai', 'openrouter', etc.

    async def complete(
        self, *,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        schema: type | None = None,  # a Pydantic BaseModel class, or None
        model: str,
        retry_on_parse_failure: bool = True,
    ) -> LLMResponse:
        ...

    async def health_check(self) -> bool:
        """Lightweight ping to verify api key + connectivity. Updates
        tenant_ai_config.api_key_last_validated_at / api_key_last_error."""
        ...


@runtime_checkable
class ActionDispatcher(Protocol):
    """One adapter per outbound channel.

    Each adapter encapsulates the channel's quirks (Resend timeouts, Twilio
    TCPA windows, LinkedIn weekly caps) so the engine itself stays
    channel-agnostic.
    """
    channel_code: str  # 'email', 'sms', 'linkedin', 'call_task', 'manual'

    async def pre_dispatch_guards(self, action) -> GuardResult:
        """Channel-specific safety checks BEFORE send.

        Email: anomaly score, suppression list, identity warmup cap,
               empty-subject guard, placeholder regex, STAGING rewrite prep.
        SMS:   TCPA quiet hours (8am-9pm local), opt-out check, E.164 format.
        LinkedIn: connection-status check, weekly cap.
        Call task: BDR assignment validation.
        Manual: always passes (BDR handles).
        """
        ...

    async def is_in_send_window(self, local_now, tcpa_b2b_override: bool) -> bool:
        """TZ-aware quiet-hours gate. Returns True if it's currently OK to
        send via this channel in the contact's local time. Dispatcher
        reschedules to next legal time if False."""
        ...

    async def send(self, action) -> SendResult:
        """Actually dispatch via the channel's underlying transport.
        Idempotency assumed already enforced by DB-level UNIQUE on
        actions.idempotency_key."""
        ...

    async def fetch_outcome(self, action) -> OutcomeUpdate | None:
        """Poll (or webhook-driven for Resend/Twilio) outcome updates.
        Returns None if no update yet. Idempotent: safe to call repeatedly."""
        ...


@runtime_checkable
class SignalSource(Protocol):
    """One adapter per inbound signal source (GMB, LinkedIn, website, etc.).

    Adapter responsibilities:
      1. Fetch current state of the source for a given URL.
      2. Diff against prior snapshot to extract one or more signals.
      3. Self-impose rate limits (respect API quotas, ToS).
    """
    source_type_code: str  # FK code into source_types lookup
    poll_interval_default_days: int

    async def fetch(self, url: str) -> Snapshot:
        """Fetch raw current state. Raises SourceError on transport failure
        (caught by signal_watcher → consecutive_failures++ + backoff)."""
        ...

    def extract_signals(
        self,
        prev_snapshot: Snapshot | None,
        current_snapshot: Snapshot,
    ) -> list[ExtractedSignal]:
        """Diff prev → current and produce zero or more ExtractedSignal.

        Returning [] when nothing meaningfully changed is the common case.
        prev=None on first poll: adapter decides what counts as "baseline
        signals" (often none — just record current state and wait for next
        poll to start diffing).
        """
        ...


# ════════════════════════════════════════════════════════════════════════════
# Exceptions
# ════════════════════════════════════════════════════════════════════════════

class EngagementEngineError(Exception):
    """Base for all engagement engine exceptions."""


class ParseError(EngagementEngineError):
    """LLM response could not be parsed against the required schema after
    retry. Caller should try fallback_provider if configured."""


class RateLimitExceeded(EngagementEngineError):
    """LLM provider returned 429 and backoff was exhausted."""


class ProviderUnavailable(EngagementEngineError):
    """LLM provider connection failed or returned 5xx."""


class CostBudgetExceeded(EngagementEngineError):
    """Atomic budget reservation failed: engagement or tenant cap hit.
    Caller should pause the engagement and notify BDR."""


class SourceError(EngagementEngineError):
    """SignalSource fetch failed (rate limit, transport, parse).
    signal_watcher increments observations.consecutive_failures + backs off."""


class TransientChannelError(EngagementEngineError):
    """Dispatch transport failed transiently; action should be rescheduled."""


class PermanentChannelError(EngagementEngineError):
    """Dispatch transport failed permanently; action marked failed."""
