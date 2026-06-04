"""AnthropicProvider — direct calls to api.anthropic.com.

Uses httpx directly (not the anthropic-python SDK) to:
  - Keep dependency surface tight
  - Maintain consistent error/retry semantics with other providers
  - Avoid SDK-version coupling

Cost reporting: Anthropic API returns `usage.input_tokens` and
`usage.output_tokens`. We compute cost via our static price table since
Anthropic doesn't include dollar cost in the response. Cache hits show as
`cache_read_input_tokens` which are billed at 10% — for Phase 4 we treat
all tokens as full-price (overcounting); cache-aware accounting lands
later if/when we use the prompt-caching feature.
"""
from __future__ import annotations
import logging
import os
from typing import Any

import httpx

from app.engagement_engine.interfaces import (
    LLMResponse,
    ProviderUnavailable,
)
from app.engagement_engine.llm_providers.base import (
    RawCompletionResult,
    classify_http_error,
    standard_complete_flow,
    with_rate_limit_retry,
)

log = logging.getLogger("engagement_engine.llm.anthropic")

ANTHROPIC_API_BASE = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_TIMEOUT = 60.0  # the decision_maker calls can take 30s+

# Models that no longer accept the `temperature` parameter (Anthropic
# deprecated it on the Opus 4.7+ family — the API returns HTTP 400
# 'temperature is deprecated for this model' when included). We omit
# temperature for these models; they use a fixed internal value.
MODELS_NO_TEMPERATURE: set[str] = {
    "claude-opus-4-7",
    "claude-opus-4-8",
}


class AnthropicProvider:
    """Direct Anthropic API adapter. Used by tenant_ai_config.provider='anthropic'
    AND wrapped by the AAMPDefaultProvider."""

    name: str = "anthropic"

    def __init__(self, api_key: str, base_url: str | None = None):
        if not api_key:
            raise ValueError("AnthropicProvider requires non-empty api_key")
        self.api_key = api_key
        self.base_url = base_url or ANTHROPIC_API_BASE

    async def complete(
        self, *,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        schema: type | None = None,
        model: str,
        retry_on_parse_failure: bool = True,
    ) -> LLMResponse:
        return await with_rate_limit_retry(
            lambda: standard_complete_flow(
                provider_name=self.name,
                raw_complete_fn=self._raw_complete,
                prompt=prompt, system=system,
                max_tokens=max_tokens, temperature=temperature,
                schema=schema, model=model,
                retry_on_parse_failure=retry_on_parse_failure,
            ),
        )

    async def _raw_complete(
        self, *, prompt: str, system: str | None,
        max_tokens: int, temperature: float, model: str,
    ) -> RawCompletionResult:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        # Opus 4.7+ family rejects the temperature param entirely.
        if model not in MODELS_NO_TEMPERATURE:
            payload["temperature"] = temperature
        if system:
            payload["system"] = system

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.post(
                    self.base_url, json=payload, headers=headers,
                )
        except httpx.HTTPError as e:
            raise ProviderUnavailable(
                f"anthropic transport: {type(e).__name__}: {e}"
            ) from e

        if response.status_code != 200:
            raise classify_http_error(response.status_code, response.text)

        data = response.json()

        # Extract text content from Anthropic's content-block format
        content_blocks = data.get("content", [])
        text_parts = [
            block.get("text", "") for block in content_blocks
            if block.get("type") == "text"
        ]
        text = "".join(text_parts)

        usage = data.get("usage", {}) or {}
        return RawCompletionResult(
            content=text,
            tokens_in=usage.get("input_tokens"),
            tokens_out=usage.get("output_tokens"),
            cost_usd=None,  # Anthropic doesn't report cost; fallback computes
            model_used=data.get("model", model),
        )

    async def health_check(self) -> bool:
        """Lightweight ping. Anthropic doesn't have a dedicated health
        endpoint, so we send a 1-token completion."""
        try:
            await self.complete(
                prompt="ping",
                max_tokens=1,
                temperature=0.0,
                model="claude-haiku-4-5",
                schema=None,
                retry_on_parse_failure=False,
            )
            return True
        except Exception as e:
            log.warning("anthropic health_check failed: %s", e)
            return False


class AAMPDefaultProvider(AnthropicProvider):
    """Wraps AnthropicProvider using AAMP's platform API key from env.

    Used when tenant_ai_config.provider='aamp_default'. Tenants get
    out-of-box AI without needing to bring their own key; billed back at
    a markup.
    """
    name: str = "aamp_default"

    def __init__(self):
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError(
                "AAMP_DEFAULT requires ANTHROPIC_API_KEY env var"
            )
        super().__init__(api_key=api_key)
