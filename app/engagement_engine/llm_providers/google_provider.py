"""GoogleGeminiProvider — calls to generativelanguage.googleapis.com.

Phase 4 minimum implementation. Google's API shape differs from OpenAI's;
we translate generation parameters at the adapter layer so the engine's
LLMProvider interface stays uniform.
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

log = logging.getLogger("engagement_engine.llm.google")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_TIMEOUT = 60.0


class GoogleGeminiProvider:
    name: str = "google_gemini"

    def __init__(self, api_key: str, base_url: str | None = None):
        if not api_key:
            raise ValueError("GoogleGeminiProvider requires non-empty api_key")
        self.api_key = api_key
        self.base_url = base_url or GEMINI_BASE

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
        url = f"{self.base_url}/{model}:generateContent?key={self.api_key}"

        # Gemini uses `system_instruction` separate from `contents`.
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system:
            payload["systemInstruction"] = {
                "parts": [{"text": system}],
            }

        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
        except httpx.HTTPError as e:
            raise ProviderUnavailable(
                f"google transport: {type(e).__name__}: {e}"
            ) from e

        if response.status_code != 200:
            raise classify_http_error(response.status_code, response.text)

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise ProviderUnavailable(f"google empty candidates: {str(data)[:300]}")

        # Extract text from the first candidate's parts
        parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)

        usage = data.get("usageMetadata") or {}
        return RawCompletionResult(
            content=text,
            tokens_in=usage.get("promptTokenCount"),
            tokens_out=usage.get("candidatesTokenCount"),
            cost_usd=None,  # Google doesn't return cost; base estimates
            model_used=model,
        )

    async def health_check(self) -> bool:
        try:
            await self.complete(
                prompt="ping",
                max_tokens=1,
                temperature=0.0,
                model="gemini-2-flash",
                schema=None,
                retry_on_parse_failure=False,
            )
            return True
        except Exception as e:
            log.warning("google health_check failed: %s", e)
            return False
