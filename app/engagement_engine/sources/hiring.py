"""HiringSource — Indeed + Glassdoor company-page scans.

PHASE 3 STATUS: stub. Returns empty signals.

Indeed (and to a lesser extent Glassdoor) actively block scraping. A real
implementation requires either:
  - Licensed data feed (Clay, Phantombuster) — Phase 8
  - SerpAPI / similar gateway service
  - Headless browser with rotating residential proxies (operationally
    expensive + ToS gray area)

WebsiteCareersSource (already implemented) handles the cheap path for
prospects whose own careers page lists open roles. This adapter exists so
the worker dispatch path can route hiring_indeed / hiring_glassdoor
observations without crashing, and so the source_types lookup table
maps to a real class. When the licensed feed lands in Phase 8, swap the
fetch / extract_signals bodies; the worker doesn't need to change.
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
from app.engagement_engine.sources.base import hash_snapshot

log = logging.getLogger("engagement_engine.sources.hiring")


class HiringIndeedSource:
    """Phase 3 stub. Returns a benign empty snapshot."""

    source_type_code: str = "hiring_indeed"
    poll_interval_default_days: int = 14

    async def fetch(self, url: str) -> Snapshot:
        # Returns an empty snapshot so the watcher records "polled OK"
        # without emitting signals or hitting any blocked endpoint.
        log.debug(
            "HiringIndeedSource stub: not polling %s (Phase 8 enables real fetch)",
            url,
        )
        return Snapshot(
            content_hash=hash_snapshot({"stub": True, "url": url}),
            raw_data={"stub": True, "deferred_to": "phase_8"},
            observed_at=datetime.now(timezone.utc),
        )

    def extract_signals(
        self,
        prev_snapshot: Snapshot | None,
        current_snapshot: Snapshot,
    ) -> list[ExtractedSignal]:
        return []


class HiringGlassdoorSource:
    """Phase 3 stub. Same shape as HiringIndeedSource."""

    source_type_code: str = "hiring_glassdoor"
    poll_interval_default_days: int = 14

    async def fetch(self, url: str) -> Snapshot:
        log.debug(
            "HiringGlassdoorSource stub: not polling %s (Phase 8 enables real fetch)",
            url,
        )
        return Snapshot(
            content_hash=hash_snapshot({"stub": True, "url": url}),
            raw_data={"stub": True, "deferred_to": "phase_8"},
            observed_at=datetime.now(timezone.utc),
        )

    def extract_signals(
        self,
        prev_snapshot: Snapshot | None,
        current_snapshot: Snapshot,
    ) -> list[ExtractedSignal]:
        return []
