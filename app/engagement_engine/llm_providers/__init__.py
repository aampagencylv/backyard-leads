"""LLM provider registry + tenant-aware factory.

Adapters available at launch (Rule #11 — BYO AI):
  - aamp_default  — wraps AnthropicProvider with AAMP's platform key
  - anthropic     — direct api.anthropic.com
  - openai        — direct api.openai.com
  - openrouter    — gateway to DeepSeek, Llama, Mistral, GPT, Gemini, etc.
  - google_gemini — direct generativelanguage.googleapis.com

Adding a provider:
  1. Implement the adapter class (must satisfy LLMProvider Protocol)
  2. Register in PROVIDER_CLASSES below
  3. Add the code to tenant_ai_config.provider CHECK constraint (or use
     a lookup table if we grow past a handful of providers)
"""
from __future__ import annotations
import logging
from typing import Any

from sqlalchemy import text

from app.database import async_session
from app.engagement_engine.interfaces import LLMProvider
from app.engagement_engine.llm_providers.anthropic_provider import (
    AnthropicProvider, AAMPDefaultProvider,
)
from app.engagement_engine.llm_providers.openai_provider import OpenAIProvider
from app.engagement_engine.llm_providers.openrouter_provider import OpenRouterProvider
from app.engagement_engine.llm_providers.google_provider import GoogleGeminiProvider

log = logging.getLogger("engagement_engine.llm.factory")


# (provider_code → class). All take an api_key in constructor, EXCEPT
# 'aamp_default' which reads from env. Resolver below handles both.
PROVIDER_CLASSES: dict[str, type] = {
    "aamp_default":  AAMPDefaultProvider,
    "anthropic":     AnthropicProvider,
    "openai":        OpenAIProvider,
    "openrouter":    OpenRouterProvider,
    "google_gemini": GoogleGeminiProvider,
}


def supported_providers() -> list[str]:
    return sorted(PROVIDER_CLASSES.keys())


async def get_provider_for_tenant(tenant_id: int) -> LLMProvider:
    """Resolve the LLM provider for a tenant, reading tenant_ai_config.

    Decryption flow:
      - aamp_default → no key needed (provider reads env)
      - kms_arn set → fetch via secrets vault (Phase 4 minimum: env fallback)
      - api_key_encrypted set → decrypt via Fernet (existing secrets_vault)

    Raises ValueError if tenant_ai_config row is missing or misconfigured.
    """
    async with async_session() as session:
        row = await session.execute(text("""
            SELECT provider, api_key_encrypted, api_key_kms_arn, base_url
            FROM tenant_ai_config
            WHERE tenant_id = :t
        """), {"t": tenant_id})
        config = row.first()

    if config is None:
        # No config row → default to aamp_default
        log.info(
            "no tenant_ai_config for tenant %s; using aamp_default",
            tenant_id,
        )
        return AAMPDefaultProvider()

    provider_code = config.provider
    provider_class = PROVIDER_CLASSES.get(provider_code)
    if provider_class is None:
        raise ValueError(
            f"tenant {tenant_id} has unsupported provider: {provider_code!r} "
            f"(supported: {supported_providers()})"
        )

    if provider_code == "aamp_default":
        # No api key resolution needed
        return provider_class()

    # Resolve api key
    api_key = await _resolve_api_key(
        kms_arn=config.api_key_kms_arn,
        encrypted=config.api_key_encrypted,
        tenant_id=tenant_id,
    )
    if not api_key:
        raise ValueError(
            f"tenant {tenant_id} provider={provider_code} has no resolvable api_key"
        )

    base_url = config.base_url or None
    return provider_class(api_key=api_key, base_url=base_url)


async def _resolve_api_key(
    *, kms_arn: str | None, encrypted: str | None, tenant_id: int,
) -> str | None:
    """Decrypt the tenant's API key.

    Phase 4 minimum: if encrypted blob is present, decrypt via the existing
    secrets_vault module. If KMS arn is set but encrypted blob is absent,
    log a warning and return None — full KMS fetch lands in Phase 5+ along
    with key rotation.
    """
    if encrypted:
        try:
            from app.secrets_vault import decrypt_secret
            return decrypt_secret(encrypted)
        except Exception as e:
            log.error(
                "secrets_vault decrypt failed for tenant %s: %s", tenant_id, e,
            )
            return None
    if kms_arn:
        log.warning(
            "tenant %s uses kms_arn but KMS-fetch not yet implemented "
            "(Phase 4 minimum decrypts encrypted_blob only); api call will fail",
            tenant_id,
        )
        return None
    return None


# ── Model selection helper ──────────────────────────────────────────────────

async def get_model_for_decision_type(
    tenant_id: int, decision_type: str,
) -> str:
    """Map a decision_type to the tenant's configured model.

    Per design Rule #11: tenants pick which model handles which task type.
    Cheap tasks (signal_scoring, reply_classification) use the cheap model;
    expensive tasks use the expensive model. The mapping is in
    tenant_ai_config.

    Mapping per the design doc:
      - score_signal_relevance, classify_reply, recommend_*,
        detect_fatigue → model_signal_scoring
      - generate_engagement_summary, select_next_step,
        generate_content → model_content_generation
      - what_to_send, when_to_send, draft_reply,
        recommend_phase_transition, recommend_playbook_switch → model_decision_making
    """
    async with async_session() as session:
        row = await session.execute(text("""
            SELECT model_signal_scoring, model_reply_classification,
                   model_content_generation, model_decision_making,
                   model_engagement_summary
            FROM tenant_ai_config
            WHERE tenant_id = :t
        """), {"t": tenant_id})
        config = row.first()

    if config is None:
        # Default to Claude tier mapping
        defaults = {
            "model_signal_scoring": "claude-haiku-4-5",
            "model_reply_classification": "claude-haiku-4-5",
            "model_content_generation": "claude-sonnet-4-6",
            "model_decision_making": "claude-opus-4-7",
            "model_engagement_summary": "claude-sonnet-4-6",
        }
        config_attr = defaults.get
    else:
        config_attr = lambda k, default=None: getattr(config, k, default)

    # decision_type → which column
    cheap_types = {
        "score_signal_relevance", "recommend_tier_change",
        "recommend_pause", "detect_fatigue",
    }
    medium_types = {
        "select_next_step", "generate_content",
    }
    expensive_types = {
        "what_to_send", "when_to_send", "draft_reply",
        "recommend_phase_transition", "recommend_playbook_switch",
    }

    if decision_type == "classify_reply":
        return config_attr("model_reply_classification")
    if decision_type == "generate_engagement_summary":
        return config_attr("model_engagement_summary")
    if decision_type in cheap_types:
        return config_attr("model_signal_scoring")
    if decision_type in medium_types:
        return config_attr("model_content_generation")
    if decision_type in expensive_types:
        return config_attr("model_decision_making")

    # Unknown decision_type: default to content_generation
    log.warning(
        "unknown decision_type %r for model selection; using content_generation tier",
        decision_type,
    )
    return config_attr("model_content_generation")
