"""Pluggable signal source adapters.

Each adapter implements `SignalSource` and is registered here by
source_type_code. The signal_watcher looks up the adapter for an
observation via:
    adapter = get_source(observation.source_type_code)
    snapshot = await adapter.fetch(observation.source_url)
    signals = adapter.extract_signals(prev_snapshot, snapshot)

Adding a new source:
  1. Create the adapter class implementing SignalSource
  2. Add to SOURCE_REGISTRY below
  3. INSERT INTO source_types (code, label, adapter_class, default_poll_days)
  4. Workers receive LISTEN/NOTIFY refresh on the next tick
"""
from __future__ import annotations
from app.engagement_engine.interfaces import SignalSource
from app.engagement_engine.sources.gmb import GMBListingSource
from app.engagement_engine.sources.website import (
    WebsiteHomepageSource,
    WebsiteCareersSource,
)
from app.engagement_engine.sources.hiring import (
    HiringIndeedSource,
    HiringGlassdoorSource,
)


SOURCE_REGISTRY: dict[str, SignalSource] = {
    "gmb_listing":       GMBListingSource(),
    "website_homepage":  WebsiteHomepageSource(),
    "website_careers":   WebsiteCareersSource(),
    "hiring_indeed":     HiringIndeedSource(),       # Phase 3 stub
    "hiring_glassdoor":  HiringGlassdoorSource(),    # Phase 3 stub
    # 'linkedin_profile', 'linkedin_company', 'linkedin_posts' — Phase 8
    # 'news_mentions', 'yelp_listing', 'facebook_page', 'instagram_profile'
    #   — future, source_types row already seeded
}


def get_source(source_type_code: str) -> SignalSource | None:
    """Look up the adapter for a source type. Returns None if not registered;
    watcher marks the observation as failing with reason='no_adapter'."""
    return SOURCE_REGISTRY.get(source_type_code)


def supported_source_types() -> list[str]:
    """List of source type codes the engine can currently poll (including
    Phase 3 stubs)."""
    return sorted(SOURCE_REGISTRY.keys())
