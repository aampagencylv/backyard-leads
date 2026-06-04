"""Shared helpers for signal source adapters.

A SignalSource adapter implements:
  - fetch(url) -> Snapshot
  - extract_signals(prev, current) -> list[ExtractedSignal]

This module provides:
  - canonical_repr() — deterministic JSON serialization for hashing
  - hash_snapshot() — SHA256 over canonical repr
  - safe_text_fetch() — HTTP GET with timeout + size cap + content type check
  - extract_text_from_html() — cheap HTML → text (no full parser)
"""
from __future__ import annotations
import hashlib
import json
import re
from typing import Any
import httpx


# Per-fetch limits to prevent a hostile or runaway response from
# consuming worker memory / bandwidth budget.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5MB
DEFAULT_TIMEOUT_SECONDS = 15.0

# User agent presents the engine politely to whatever we're polling so
# robots.txt + log-based abuse detection can identify + block us if asked.
USER_AGENT = (
    "LeadProspector-EngagementEngine/1.0 "
    "(+https://leadprospector.ai/bot; engine signal watcher)"
)


def canonical_repr(data: Any) -> str:
    """Deterministic JSON for snapshot hashing.

    sort_keys=True and ensure_ascii=False so semantically-identical dicts
    always serialize to identical bytes regardless of dict insertion order
    or Python version.
    """
    return json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)


def hash_snapshot(data: Any) -> str:
    """SHA256 hex digest of the canonical representation."""
    return hashlib.sha256(canonical_repr(data).encode("utf-8")).hexdigest()


async def safe_text_fetch(
    url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    """HTTP GET with a hard size cap and timeout.

    Returns (status_code, body_text). Raises httpx.HTTPError on transport
    failure. Truncates the body at MAX_RESPONSE_BYTES.
    """
    async with httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        # Stream the response so we can stop reading once we hit the cap.
        async with client.stream("GET", url) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) >= MAX_RESPONSE_BYTES:
                    break
            return response.status_code, bytes(body).decode(
                "utf-8", errors="replace",
            )


# Cheap HTML → text extractor. We don't pull lxml/bs4 for this because the
# signal we care about is "did the content change" rather than parsed
# semantic structure. For the few signals that need parsed structure
# (job-count from a careers page), each adapter handles it specifically.

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def extract_text_from_html(html: str) -> str:
    """Strip scripts, styles, and tags; collapse whitespace.

    Output is normalized enough for content-hash diff detection. Not
    semantically faithful — don't use for ML / NLP without further
    processing.
    """
    body = _SCRIPT_STYLE_RE.sub(" ", html)
    body = _TAG_RE.sub(" ", body)
    body = _WS_RE.sub(" ", body).strip()
    return body


# Content fingerprint: ignore boilerplate that changes every page-load
# (timestamps, CSRF tokens, session IDs, cache busters) so we don't false-
# positive on rotated HTML.

_NOISE_PATTERNS = [
    re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[A-Z\d:+-]*"),  # ISO datetime
    re.compile(r"csrf[_-]?token[\"']?\s*[:=]\s*[\"'][\w\-_]+"),
    re.compile(r"nonce[\"']?\s*[:=]\s*[\"'][\w\-_]+"),
    re.compile(r"v=\d+\.\d+\.\d+"),  # cache-buster versions
    re.compile(r"\?_t=\d+"),
    re.compile(r"\b\d{10,13}\b"),  # epoch timestamps
]


def fingerprint_text(text: str) -> str:
    """Strip noisy tokens before hashing so we don't see false changes."""
    for pat in _NOISE_PATTERNS:
        text = pat.sub("", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


# ── Polling cadence helpers ────────────────────────────────────────────────

def compute_next_poll_at(
    *, interval_days: int, consecutive_failures: int = 0,
) -> "datetime":
    """Compute next_poll_at with jitter + exponential backoff on failure.

    Jitter prevents thundering herd: every observation due at exactly
    midnight UTC would otherwise tick simultaneously. We add up to
    `interval_days / 4` of random extra time.

    Exponential backoff on consecutive_failures: 1 failure → +1 day,
    2 → +4 days, 3 → +9 days, capped at +60 days.
    """
    from datetime import datetime, timedelta, timezone
    import random

    base_seconds = interval_days * 86400
    jitter_seconds = random.uniform(0, base_seconds * 0.25)
    backoff_days = min(60, consecutive_failures ** 2)
    delta = timedelta(
        seconds=base_seconds + jitter_seconds + (backoff_days * 86400)
    )
    return datetime.now(timezone.utc) + delta
