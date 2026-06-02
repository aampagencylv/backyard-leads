"""
Central Anthropic client + model tiering + prompt caching helpers.

Why a wrapper:
  * One place to centralize model IDs — when a new Sonnet/Haiku ships,
    change here, every caller benefits.
  * Tier picker so callers think in capability ("fast classifier",
    "balanced writer", "heavy reasoner") instead of model name.
  * Prompt-caching helper. Anthropic charges 10% of input price on
    cached prefixes — for our long-system-prompt callsites (email
    generation), this is a 5-10x cost reduction once warm.

Tier guidance:
  MODEL_FAST     — Haiku 4.5. Use for: classification, scoring,
                   extraction, short summaries, anything where you
                   would have used a logistic regression a few years ago.
  MODEL_BALANCED — Sonnet 4.6. Use for: writing emails, generating
                   replies, multi-step reasoning, anything user-facing
                   where quality is the bar.
  MODEL_HEAVY    — Opus 4.8. Use only when Sonnet's output is
                   demonstrably insufficient. ~5x cost of Sonnet.

Prompt caching:
  Pass `cacheable=True` to `chat_with_system()` and the system prompt
  is sent with `cache_control: ephemeral`. The first call pays full
  price; subsequent calls within 5 minutes pay 10% on the cached part.
  TTL is rolling — every cache hit refreshes it.
"""
from __future__ import annotations
from typing import Any, Optional

import anthropic

from app.config import settings


# Current-generation model IDs. Update both halves when a new family ships.
MODEL_FAST = "claude-haiku-4-5-20251001"
MODEL_BALANCED = "claude-sonnet-4-6"
MODEL_HEAVY = "claude-opus-4-8"


def get_client() -> anthropic.AsyncAnthropic:
    """Return an AsyncAnthropic client configured with the platform key.

    Tenant-byo Anthropic keys (Enterprise plan) will read from the
    tenant_secrets vault here once that work lands. For now everything
    uses the platform key.
    """
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


async def chat_with_system(
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 512,
    cacheable: bool = False,
    temperature: Optional[float] = None,
) -> str:
    """Single-shot chat with a system prompt. Returns the assistant text.

    When `cacheable=True`, the system block carries
    `cache_control: ephemeral`. Anthropic caches the prefix for ~5 min
    and bills cache hits at 10% of normal input price. Best for system
    prompts that don't change between calls (templates, classifier
    rubrics, persona instructions).
    """
    client = get_client()
    if cacheable:
        system_param: Any = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
    else:
        system_param = system

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_param,
        "messages": [{"role": "user", "content": user}],
    }
    if temperature is not None:
        kwargs["temperature"] = temperature

    response = await client.messages.create(**kwargs)
    return response.content[0].text
