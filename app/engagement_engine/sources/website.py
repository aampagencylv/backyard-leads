"""WebsiteHomepageSource — polls a prospect company's homepage for changes.

Strategy:
  1. Fetch the URL with safe_text_fetch (size + timeout caps)
  2. Strip scripts, styles, tags, and noise (timestamps, CSRF tokens)
  3. Hash the resulting fingerprint
  4. If hash differs from prior snapshot, emit a 'website_change' signal
     with the diff stats

We DO NOT attempt to render JavaScript-driven content. SPA-only homepages
(rare for our prospect segment — local home-services + B2C) produce only
the initial shell as a snapshot. If we need JS rendering later it's
~$0.10 per fetch via a headless service; defer to Phase 8 if/when needed.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from app.engagement_engine.interfaces import (
    SignalSource,
    Snapshot,
    ExtractedSignal,
    SourceError,
)
from app.engagement_engine.sources.base import (
    safe_text_fetch,
    extract_text_from_html,
    fingerprint_text,
    hash_snapshot,
)

log = logging.getLogger("engagement_engine.sources.website")


class WebsiteHomepageSource:
    """Polls a homepage. Emits `website_change` when content fingerprint
    differs from the last snapshot."""

    source_type_code: str = "website_homepage"
    poll_interval_default_days: int = 14

    async def fetch(self, url: str) -> Snapshot:
        try:
            status, body = await safe_text_fetch(url)
        except Exception as e:
            raise SourceError(
                f"website fetch failed for {url}: {type(e).__name__}: {e}"
            ) from e

        if status >= 500:
            raise SourceError(
                f"website returned {status} for {url} (transient)"
            )

        text = extract_text_from_html(body)
        fingerprint = fingerprint_text(text)
        content_hash = hash_snapshot({"fingerprint": fingerprint})

        return Snapshot(
            content_hash=content_hash,
            raw_data={
                "status_code": status,
                "url": url,
                "text_length": len(text),
                "fingerprint_length": len(fingerprint),
                # Store just the first 4KB for diff inspection in the UI; we
                # explicitly DO NOT store full HTML (raw third-party content
                # exposure + storage bloat — design rule on raw_data_json).
                "text_preview": text[:4096],
            },
            observed_at=datetime.now(timezone.utc),
        )

    def extract_signals(
        self,
        prev_snapshot: Snapshot | None,
        current_snapshot: Snapshot,
    ) -> list[ExtractedSignal]:
        if prev_snapshot is None:
            # First poll — record state, no signal yet (no baseline to diff)
            return []
        if prev_snapshot.content_hash == current_snapshot.content_hash:
            return []

        prev_len = (prev_snapshot.raw_data or {}).get("text_length", 0)
        curr_len = current_snapshot.raw_data.get("text_length", 0)
        delta_chars = curr_len - prev_len
        delta_pct = (delta_chars / max(1, prev_len)) * 100

        # Don't emit a signal if the change is below a noise floor — most
        # site updates have meaningful size deltas. <5% change is usually a
        # rotated quote / news ticker / counter widget.
        if abs(delta_pct) < 5:
            return []

        return [
            ExtractedSignal(
                signal_type_code="website_change",
                extracted_facts={
                    "delta_chars": delta_chars,
                    "delta_pct": round(delta_pct, 1),
                    "prev_text_length": prev_len,
                    "current_text_length": curr_len,
                    "direction": "added" if delta_chars > 0 else "removed",
                },
                source_url=current_snapshot.raw_data.get("url"),
            )
        ]


class WebsiteCareersSource:
    """Polls a /careers or /jobs page. Emits `hiring_signal` when the page
    text mentions more jobs or new role keywords than the prior snapshot.

    Phase 3 implementation is a cheap word-counter heuristic: count
    occurrences of role keywords (engineer, designer, manager, etc.) and
    emit a signal when the count increases by ≥2.

    Phase 8: replace with the Clay / licensed-data path which gives
    structured job postings.
    """

    source_type_code: str = "website_careers"
    poll_interval_default_days: int = 14

    # Common role keywords. Counting these gives a rough hiring-signal that
    # works for most prospects without ML / parsing.
    ROLE_KEYWORDS = (
        "engineer", "developer", "designer", "manager", "director",
        "specialist", "coordinator", "analyst", "consultant",
        "representative", "associate", "lead", "supervisor",
    )

    async def fetch(self, url: str) -> Snapshot:
        try:
            status, body = await safe_text_fetch(url)
        except Exception as e:
            raise SourceError(
                f"careers fetch failed for {url}: {type(e).__name__}: {e}"
            ) from e
        if status >= 500:
            raise SourceError(
                f"careers returned {status} for {url} (transient)"
            )
        if status == 404:
            # Page was deleted/moved — flag as such, no signal extraction
            raise SourceError(f"careers 404 for {url} (consider deactivating)")

        text = extract_text_from_html(body).lower()
        # Cheap role-keyword tally
        role_count = sum(text.count(kw) for kw in self.ROLE_KEYWORDS)
        content_hash = hash_snapshot({
            "role_count": role_count,
            "text_fp": fingerprint_text(text)[:8192],
        })
        return Snapshot(
            content_hash=content_hash,
            raw_data={
                "status_code": status,
                "url": url,
                "role_keyword_count": role_count,
                "text_length": len(text),
            },
            observed_at=datetime.now(timezone.utc),
        )

    def extract_signals(
        self,
        prev_snapshot: Snapshot | None,
        current_snapshot: Snapshot,
    ) -> list[ExtractedSignal]:
        if prev_snapshot is None:
            return []
        prev_count = (prev_snapshot.raw_data or {}).get("role_keyword_count", 0)
        curr_count = current_snapshot.raw_data.get("role_keyword_count", 0)
        delta = curr_count - prev_count
        if delta < 2:
            return []  # noise floor — single-role rotation is normal
        return [
            ExtractedSignal(
                signal_type_code="hiring_signal",
                extracted_facts={
                    "prev_role_count": prev_count,
                    "current_role_count": curr_count,
                    "delta": delta,
                },
                source_url=current_snapshot.raw_data.get("url"),
            )
        ]
