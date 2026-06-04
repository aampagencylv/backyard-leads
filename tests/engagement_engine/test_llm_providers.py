"""Tests for LLM provider adapters — initialization + parsing.

Live LLM calls require API keys and are NOT exercised here. Those are
validated on staging via the decision_maker tick. These tests cover:
  - Each provider class instantiates with valid keys, rejects empty key
  - extract_json_blob handles markdown fences, prose-wrapped JSON,
    balanced-brace scan, pure-JSON, malformed
  - parse_to_schema enforces Pydantic schemas + extra='forbid'
  - build_repair_prompt produces a corrective prompt referencing the schema
"""
import pytest
from pydantic import BaseModel, Field, ConfigDict

from app.engagement_engine.interfaces import ParseError
from app.engagement_engine.llm_providers.base import (
    extract_json_blob,
    parse_to_schema,
    build_repair_prompt,
)
from app.engagement_engine.llm_providers.anthropic_provider import (
    AnthropicProvider, AAMPDefaultProvider,
)
from app.engagement_engine.llm_providers.openrouter_provider import OpenRouterProvider
from app.engagement_engine.llm_providers.openai_provider import OpenAIProvider
from app.engagement_engine.llm_providers.google_provider import GoogleGeminiProvider
from app.engagement_engine.llm_providers import (
    PROVIDER_CLASSES, supported_providers, get_model_for_decision_type,
)


# ── Provider initialization ────────────────────────────────────────────────

def test_anthropic_provider_requires_api_key():
    with pytest.raises(ValueError, match="non-empty api_key"):
        AnthropicProvider(api_key="")


def test_anthropic_provider_initializes_with_key():
    p = AnthropicProvider(api_key="sk-ant-test")
    assert p.name == "anthropic"
    assert p.api_key == "sk-ant-test"


def test_openrouter_provider_requires_api_key():
    with pytest.raises(ValueError, match="non-empty api_key"):
        OpenRouterProvider(api_key="")


def test_openai_provider_requires_api_key():
    with pytest.raises(ValueError, match="non-empty api_key"):
        OpenAIProvider(api_key="")


def test_google_provider_requires_api_key():
    with pytest.raises(ValueError, match="non-empty api_key"):
        GoogleGeminiProvider(api_key="")


def test_aamp_default_provider_reads_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    p = AAMPDefaultProvider()
    assert p.name == "aamp_default"
    assert p.api_key == "sk-ant-from-env"


def test_aamp_default_provider_fails_without_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        AAMPDefaultProvider()


def test_custom_base_url_supported():
    """Tenants on Azure OpenAI / Bedrock proxy need to override base_url."""
    p = AnthropicProvider(api_key="x", base_url="https://my-proxy.example/v1/messages")
    assert p.base_url == "https://my-proxy.example/v1/messages"


# ── Registry ───────────────────────────────────────────────────────────────

def test_supported_providers_lists_all_five():
    assert set(supported_providers()) == {
        "aamp_default", "anthropic", "openai",
        "openrouter", "google_gemini",
    }


def test_provider_classes_mapping_correct():
    assert PROVIDER_CLASSES["anthropic"] is AnthropicProvider
    assert PROVIDER_CLASSES["openrouter"] is OpenRouterProvider
    assert PROVIDER_CLASSES["openai"] is OpenAIProvider
    assert PROVIDER_CLASSES["google_gemini"] is GoogleGeminiProvider
    assert PROVIDER_CLASSES["aamp_default"] is AAMPDefaultProvider


# ── extract_json_blob ──────────────────────────────────────────────────────

def test_extract_json_from_markdown_fence():
    raw = 'Here is my response:\n```json\n{"score": 85, "summary": "x"}\n```\nAll done.'
    assert extract_json_blob(raw) == '{"score": 85, "summary": "x"}'


def test_extract_json_balanced_brace_no_fence():
    raw = 'My answer: {"score": 50, "summary": "neutral"} thanks.'
    assert extract_json_blob(raw) == '{"score": 50, "summary": "neutral"}'


def test_extract_json_pure_json():
    raw = '{"score": 90, "summary": "urgent"}'
    assert extract_json_blob(raw) == '{"score": 90, "summary": "urgent"}'


def test_extract_json_nested_braces():
    raw = '```json\n{"outer": {"inner": {"deep": 1}}}\n```'
    assert '"outer"' in extract_json_blob(raw)


def test_extract_json_returns_none_on_no_match():
    raw = "Sorry, I cannot help with that."
    assert extract_json_blob(raw) is None


def test_extract_json_with_prefix_chatter():
    raw = "Let me think... actually, here's my JSON answer:\n\n{\"x\": 1}"
    assert extract_json_blob(raw) == '{"x": 1}'


# ── parse_to_schema ────────────────────────────────────────────────────────

class _ExampleSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: int = Field(ge=0, le=100)
    summary: str


def test_parse_to_schema_valid():
    raw = '{"score": 75, "summary": "good"}'
    parsed = parse_to_schema(raw, _ExampleSchema)
    assert parsed.score == 75
    assert parsed.summary == "good"


def test_parse_to_schema_rejects_extra_fields():
    raw = '{"score": 50, "summary": "x", "sneaky": true}'
    with pytest.raises(ParseError, match="schema validation"):
        parse_to_schema(raw, _ExampleSchema)


def test_parse_to_schema_rejects_out_of_range():
    raw = '{"score": 200, "summary": "x"}'
    with pytest.raises(ParseError, match="schema validation"):
        parse_to_schema(raw, _ExampleSchema)


def test_parse_to_schema_rejects_no_json():
    raw = "I cannot answer this question."
    with pytest.raises(ParseError, match="no JSON"):
        parse_to_schema(raw, _ExampleSchema)


def test_parse_to_schema_rejects_malformed_json():
    raw = '{"score": 75, "summary": "broken'  # unterminated string
    # Unbalanced braces → extractor can't isolate a JSON object → "no JSON"
    with pytest.raises(ParseError, match="no JSON"):
        parse_to_schema(raw, _ExampleSchema)


def test_parse_to_schema_rejects_balanced_but_invalid_json():
    """Balanced braces but malformed JSON content → JSON decode error."""
    raw = '{score: 75}'  # unquoted key — extractor finds braces, json.loads fails
    with pytest.raises(ParseError, match="JSON decode"):
        parse_to_schema(raw, _ExampleSchema)


def test_parse_to_schema_extracts_from_fence():
    raw = '```json\n{"score": 80, "summary": "good"}\n```'
    parsed = parse_to_schema(raw, _ExampleSchema)
    assert parsed.score == 80


# ── build_repair_prompt ────────────────────────────────────────────────────

def test_build_repair_prompt_includes_schema_and_error():
    original = "Score this signal: ..."
    error = ParseError("schema validation failed: bad value")
    repair = build_repair_prompt(original, _ExampleSchema, error)
    assert "bad value" in repair
    assert "score" in repair  # schema property name
    assert "summary" in repair
    assert original in repair
    assert "valid json" in repair.lower()


# ── Model selection ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_model_for_unknown_tenant_returns_claude_defaults():
    """Missing tenant_ai_config falls back to Claude tier mapping."""
    # tenant_id 999999 has no config row
    model = await get_model_for_decision_type(999999, "score_signal_relevance")
    assert "haiku" in model.lower()
    model = await get_model_for_decision_type(999999, "what_to_send")
    assert "opus" in model.lower()
    model = await get_model_for_decision_type(999999, "generate_content")
    assert "sonnet" in model.lower()
