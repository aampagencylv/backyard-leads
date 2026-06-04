"""OpenAIProvider — direct calls to api.openai.com.

Phase 4 minimum implementation: same OpenAI-compatible chat-completions
shape as OpenRouter but pointed at api.openai.com. Most tenants who want
ChatGPT will route through OpenRouter for unified billing; this direct
adapter exists for tenants with enterprise OpenAI contracts.
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

log = logging.getLogger("engagement_engine.llm.openai")

OPENAI_BASE = "https://api.openai.com/v1/chat/completions"
DEFAULT_TIMEOUT = 60.0


class OpenAIProvider:
    name: str = "openai"

    def __init__(self, api_key: str, base_url: str | None = None):
        if not api_key:
            raise ValueError("OpenAIProvider requires non-empty api_key")
        self.api_key = api_key
        self.base_url = base_url or OPENAI_BASE

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

        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.post(
                    self.base_url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as e:
            raise ProviderUnavailable(
                f"openai transport: {type(e).__name__}: {e}"
            ) from e

        if response.status_code != 200:
            raise classify_http_error(response.status_code, response.text)

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise ProviderUnavailable(f"openai empty choices: {str(data)[:300]}")
        text = (choices[0].get("message") or {}).get("content", "")
        usage = data.get("usage") or {}
        return RawCompletionResult(
            content=text,
            tokens_in=usage.get("prompt_tokens"),
            tokens_out=usage.get("completion_tokens"),
            cost_usd=None,  # OpenAI doesn't return cost; base estimates
            model_used=data.get("model") or model,
        )

    async def health_check(self) -> bool:
        try:
            await self.complete(
                prompt="ping",
                max_tokens=1,
                temperature=0.0,
                model="gpt-4o-mini",
                schema=None,
                retry_on_parse_failure=False,
            )
            return True
        except Exception as e:
            log.warning("openai health_check failed: %s", e)
            return False
