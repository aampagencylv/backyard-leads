"""OpenRouterProvider — gateway to DeepSeek, Llama, Mistral, GPT, Claude,
Gemini, and dozens of other models behind a single API key.

OpenRouter speaks an OpenAI-compatible chat-completions API.

Why ship this in Phase 4: it's the BYO AI cost-optimization path. A tenant
who picks OpenRouter + DeepSeek V3 pays $0.27/M input + $1.10/M output vs
Claude Opus's $15/$75. Same workload at <2% the cost. The trade is
quality — but for the cheap decisions (signal scoring, reply intent
classification) DeepSeek is plenty good.

Pricing: OpenRouter sends `usage.cost` in the response when configured,
which we capture directly (no static-table fallback needed for known
models). For models without reported cost, the base flow falls back to
our STATIC_PRICE_TABLE_USD_PER_M_TOKENS.
"""
from __future__ import annotations
import logging
from typing import Any

import httpx

from app.engagement_engine.interfaces import (
    LLMResponse, ProviderUnavailable,
)
from app.engagement_engine.llm_providers.base import (
    RawCompletionResult,
    classify_http_error,
    standard_complete_flow,
    with_rate_limit_retry,
)

log = logging.getLogger("engagement_engine.llm.openrouter")

OPENROUTER_BASE = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_TIMEOUT = 90.0  # some open-source models on OpenRouter are slow


class OpenRouterProvider:
    """OpenAI-compatible chat completions via OpenRouter."""

    name: str = "openrouter"

    def __init__(self, api_key: str, base_url: str | None = None):
        if not api_key:
            raise ValueError("OpenRouterProvider requires non-empty api_key")
        self.api_key = api_key
        self.base_url = base_url or OPENROUTER_BASE

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
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # OpenRouter encourages identifying your app:
            "HTTP-Referer": "https://leadprospector.ai",
            "X-Title": "LeadProspector",
        }

        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.post(
                    self.base_url, json=payload, headers=headers,
                )
        except httpx.HTTPError as e:
            raise ProviderUnavailable(
                f"openrouter transport: {type(e).__name__}: {e}"
            ) from e

        if response.status_code != 200:
            raise classify_http_error(response.status_code, response.text)

        data = response.json()

        # OpenRouter follows OpenAI's response shape.
        choices = data.get("choices") or []
        if not choices:
            raise ProviderUnavailable(
                f"openrouter returned empty choices: {str(data)[:300]}"
            )
        text = (choices[0].get("message") or {}).get("content", "")

        usage = data.get("usage") or {}
        return RawCompletionResult(
            content=text,
            tokens_in=usage.get("prompt_tokens"),
            tokens_out=usage.get("completion_tokens"),
            # OpenRouter includes `usage.total_cost` (USD) when configured.
            # Fall back to None so base layer can estimate.
            cost_usd=usage.get("total_cost"),
            model_used=data.get("model") or model,
        )

    async def health_check(self) -> bool:
        try:
            await self.complete(
                prompt="ping",
                max_tokens=1,
                temperature=0.0,
                model="openai/gpt-4o-mini",  # cheap, fast health check
                schema=None,
                retry_on_parse_failure=False,
            )
            return True
        except Exception as e:
            log.warning("openrouter health_check failed: %s", e)
            return False
