"""Shared helpers for LLM provider adapters (Rule #11: BYO AI).

Concrete provider adapters live in sibling modules. They all share:
  - JSON-schema validation with one repair retry on parse failure
  - Cost computation: prefer provider-reported tokens, fall back to static
    price table when the provider doesn't report usage (Ollama, vLLM, errors)
  - Latency timing
  - Consistent error mapping: rate limit → RateLimitExceeded, transport
    → ProviderUnavailable, parse failure → ParseError after retry exhaustion

This module DOES NOT import any provider-specific SDK. Each adapter
imports its own dependency at module load.
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.engagement_engine.cost import estimate_cost_usd
from app.engagement_engine.interfaces import (
    LLMResponse,
    ParseError,
    RateLimitExceeded,
    ProviderUnavailable,
)

log = logging.getLogger("engagement_engine.llm")

T = TypeVar("T", bound=BaseModel)


# Provider-agnostic JSON extraction. The LLM may surround JSON output with
# markdown fences, prose, or thinking blocks. We pull the first balanced
# JSON object out of the raw response.
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json_blob(raw: str) -> str | None:
    """Find the first plausible JSON object in raw LLM output.

    Tries (in order):
      1. JSON inside ```json ... ``` fences
      2. The first {...} balanced sub-string
      3. The whole raw string (in case the model returned pure JSON)
    """
    m = _JSON_FENCE_RE.search(raw)
    if m:
        return m.group(1)
    # Balanced-brace scan
    depth = 0
    start = None
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return raw[start:i + 1]
    # Last resort
    s = raw.strip()
    if s.startswith("{") and s.endswith("}"):
        return s
    return None


def parse_to_schema(raw_response: str, schema: type[T]) -> T:
    """Parse raw LLM output to a Pydantic schema instance.

    Raises ParseError if extraction or validation fails.
    """
    blob = extract_json_blob(raw_response)
    if blob is None:
        raise ParseError(
            f"no JSON object found in response (first 200 chars: {raw_response[:200]!r})"
        )
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as e:
        raise ParseError(f"JSON decode failed: {e} (blob: {blob[:200]!r})") from e
    try:
        return schema.model_validate(data)
    except ValidationError as e:
        raise ParseError(
            f"schema validation failed: {e} (data keys: {list(data.keys())})"
        ) from e


def build_repair_prompt(
    original_prompt: str, schema: type[BaseModel], parse_error: Exception,
) -> str:
    """When the first attempt fails to parse, send a corrective prompt
    that tells the model what went wrong and shows the expected schema."""
    schema_json = json.dumps(schema.model_json_schema(), indent=2)
    return (
        f"Your previous response failed to parse correctly.\n\n"
        f"Parse error: {parse_error}\n\n"
        f"Please respond with valid JSON matching this exact schema:\n\n"
        f"```json\n{schema_json}\n```\n\n"
        f"Return ONLY the JSON object, no surrounding text or explanation. "
        f"Original task follows:\n\n{original_prompt}"
    )


@dataclass
class RawCompletionResult:
    """What a provider adapter's `_raw_complete()` returns. The base class
    handles parsing/validation; adapters only worry about transport."""
    content: str
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    model_used: str


async def standard_complete_flow(
    *,
    provider_name: str,
    raw_complete_fn,  # async callable(prompt, system, max_tokens, temperature, model) -> RawCompletionResult
    prompt: str,
    system: str | None,
    max_tokens: int,
    temperature: float,
    schema: type[T] | None,
    model: str,
    retry_on_parse_failure: bool,
) -> LLMResponse:
    """The canonical complete() flow used by every adapter.

    Adapters delegate to this after defining their own _raw_complete(). It
    handles parsing, retry, cost fallback, and LLMResponse assembly.
    """
    start = time.monotonic()
    parse_attempts = 0
    parse_succeeded = True
    parsed_value: Any = None

    # First attempt
    parse_attempts += 1
    raw = await raw_complete_fn(
        prompt=prompt, system=system,
        max_tokens=max_tokens, temperature=temperature,
        model=model,
    )

    if schema is None:
        # Free-text mode — no validation
        parsed_value = raw.content
    else:
        try:
            parsed_value = parse_to_schema(raw.content, schema)
        except ParseError as first_err:
            if not retry_on_parse_failure:
                raise
            log.info(
                "LLM parse failed on provider=%s model=%s — retrying with repair prompt",
                provider_name, model,
            )
            # Second attempt with repair prompt
            parse_attempts += 1
            repair_prompt = build_repair_prompt(prompt, schema, first_err)
            raw_retry = await raw_complete_fn(
                prompt=repair_prompt, system=system,
                max_tokens=max_tokens, temperature=temperature,
                model=model,
            )
            # Accumulate cost from both attempts
            raw = RawCompletionResult(
                content=raw_retry.content,
                tokens_in=(raw.tokens_in or 0) + (raw_retry.tokens_in or 0),
                tokens_out=(raw.tokens_out or 0) + (raw_retry.tokens_out or 0),
                cost_usd=(raw.cost_usd or 0) + (raw_retry.cost_usd or 0),
                model_used=raw_retry.model_used,
            )
            try:
                parsed_value = parse_to_schema(raw.content, schema)
            except ParseError as second_err:
                parse_succeeded = False
                raise ParseError(
                    f"LLM repair retry failed too: {second_err}"
                ) from second_err

    # Cost fallback: provider didn't report → estimate from token counts
    cost_usd = raw.cost_usd
    if cost_usd is None or cost_usd == 0:
        if raw.tokens_in is not None and raw.tokens_out is not None:
            cost_usd = estimate_cost_usd(
                provider_name, model, raw.tokens_in, raw.tokens_out,
            )

    latency_ms = int((time.monotonic() - start) * 1000)
    return LLMResponse(
        content=parsed_value,
        raw_content=raw.content,
        tokens_in=raw.tokens_in,
        tokens_out=raw.tokens_out,
        cost_usd=cost_usd,
        model_used=raw.model_used,
        provider=provider_name,
        latency_ms=latency_ms,
        parse_attempts=parse_attempts,
        parse_succeeded=parse_succeeded,
    )


# ── Common transport-error mapping ──────────────────────────────────────────

def classify_http_error(status_code: int, body: str = "") -> Exception:
    """Map HTTP status codes to our exception hierarchy. Used by httpx-
    based adapters. SDK-based adapters (like the official anthropic SDK)
    use their own exception types and may bypass this."""
    if status_code == 429:
        return RateLimitExceeded(f"rate limited (429): {body[:200]}")
    if status_code in (401, 403):
        return ProviderUnavailable(
            f"auth failed ({status_code}): check api key"
        )
    if 500 <= status_code < 600:
        return ProviderUnavailable(f"provider {status_code}: {body[:200]}")
    if status_code == 408 or status_code == 504:
        return ProviderUnavailable(f"timeout ({status_code})")
    return ProviderUnavailable(f"unexpected status {status_code}: {body[:200]}")


# ── Exponential backoff with jitter for rate-limit retries ─────────────────

async def with_rate_limit_retry(coro_fn, *, max_attempts: int = 3, base_delay: float = 1.0):
    """Retry a coroutine factory on RateLimitExceeded with exponential
    backoff. Raises the last exception after max_attempts.

    coro_fn is a no-arg callable that returns a fresh coroutine on each
    invocation (so we can retry the same call after sleep).
    """
    import random
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await coro_fn()
        except RateLimitExceeded as e:
            last_exc = e
            if attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            log.warning(
                "rate limited; sleeping %.2fs before retry %d/%d",
                delay, attempt + 2, max_attempts,
            )
            await asyncio.sleep(delay)
    if last_exc:
        raise last_exc
