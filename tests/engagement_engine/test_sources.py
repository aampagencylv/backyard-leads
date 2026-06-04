"""Tests for signal source adapters (pure-function paths).

Live HTTP fetches are NOT exercised here — those are validated on staging.
These tests cover:
  - extract_signals diff logic against synthetic Snapshot pairs
  - Registry membership
  - Hashing determinism + noise filtering
  - Polling cadence helper (compute_next_poll_at) — bounds + backoff
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.engagement_engine.interfaces import Snapshot, ExtractedSignal
from app.engagement_engine.sources import (
    get_source, supported_source_types, SOURCE_REGISTRY,
)
from app.engagement_engine.sources.base import (
    canonical_repr, hash_snapshot, fingerprint_text, compute_next_poll_at,
)
from app.engagement_engine.sources.gmb import GMBListingSource
from app.engagement_engine.sources.website import (
    WebsiteHomepageSource, WebsiteCareersSource,
)
from app.engagement_engine.sources.hiring import (
    HiringIndeedSource, HiringGlassdoorSource,
)


# ── Registry ────────────────────────────────────────────────────────────────

def test_registry_has_expected_sources():
    expected = {
        "gmb_listing", "website_homepage", "website_careers",
        "hiring_indeed", "hiring_glassdoor",
    }
    assert set(supported_source_types()) == expected


def test_get_source_returns_correct_adapter():
    assert isinstance(get_source("gmb_listing"), GMBListingSource)
    assert isinstance(get_source("website_homepage"), WebsiteHomepageSource)
    assert isinstance(get_source("website_careers"), WebsiteCareersSource)
    assert isinstance(get_source("hiring_indeed"), HiringIndeedSource)
    assert isinstance(get_source("hiring_glassdoor"), HiringGlassdoorSource)


def test_unknown_source_returns_none():
    assert get_source("not_a_source") is None
    assert get_source("linkedin_profile") is None  # Phase 8


def test_each_adapter_advertises_correct_code():
    for code, adapter in SOURCE_REGISTRY.items():
        assert adapter.source_type_code == code


# ── Hashing determinism ─────────────────────────────────────────────────────

def test_canonical_repr_dict_order_independent():
    a = {"x": 1, "y": 2, "z": {"nested": True}}
    b = {"z": {"nested": True}, "y": 2, "x": 1}
    assert canonical_repr(a) == canonical_repr(b)


def test_hash_snapshot_stable():
    a = {"name": "Acme", "rating": 4.5, "count": 150}
    assert hash_snapshot(a) == hash_snapshot(a)
    assert hash_snapshot(a) != hash_snapshot({"name": "Acme", "rating": 4.6, "count": 150})


def test_fingerprint_strips_timestamps():
    raw = "Status updated at 2026-06-04T12:34:56Z by admin"
    fp = fingerprint_text(raw)
    assert "2026" not in fp
    assert "Status updated" in fp


def test_fingerprint_strips_csrf_tokens():
    raw = 'form data csrf_token="abc123xyz789" submit'
    fp = fingerprint_text(raw)
    assert "abc123xyz789" not in fp


# ── compute_next_poll_at bounds + backoff ──────────────────────────────────

def test_next_poll_with_no_failures_returns_within_interval():
    now = datetime.now(timezone.utc)
    next_poll = compute_next_poll_at(interval_days=7)
    delta = next_poll - now
    # 7 days base + up to 25% jitter
    assert timedelta(days=7) <= delta <= timedelta(days=7) * 1.26


def test_next_poll_with_failures_backs_off():
    """Exponential backoff: failures**2 days, capped at 60."""
    now = datetime.now(timezone.utc)
    # 1 failure → +1 day
    n1 = compute_next_poll_at(interval_days=7, consecutive_failures=1)
    assert (n1 - now) >= timedelta(days=7) + timedelta(days=1)
    # 3 failures → +9 days
    n3 = compute_next_poll_at(interval_days=7, consecutive_failures=3)
    assert (n3 - now) >= timedelta(days=7) + timedelta(days=9)
    # 100 failures capped at +60 days extra
    n100 = compute_next_poll_at(interval_days=7, consecutive_failures=100)
    assert (n100 - now) <= timedelta(days=7) * 1.26 + timedelta(days=60)


def test_jitter_introduces_variance():
    """Two calls in a row should yield different next_poll_at thanks to
    random jitter."""
    times = [compute_next_poll_at(interval_days=14) for _ in range(5)]
    assert len(set(times)) > 1


# ── GMBListingSource.extract_signals ───────────────────────────────────────

def _snap(content_hash="h0", **raw) -> Snapshot:
    return Snapshot(
        content_hash=content_hash,
        raw_data=raw,
        observed_at=datetime.now(timezone.utc),
    )


def test_gmb_first_poll_no_signals():
    src = GMBListingSource()
    snapshot = _snap(name="X", rating=4.5, user_ratings_total=10)
    signals = src.extract_signals(prev_snapshot=None, current_snapshot=snapshot)
    assert signals == []


def test_gmb_review_count_increase_emits_signals_per_review():
    src = GMBListingSource()
    prev = _snap("h0", name="X", rating=4.5, user_ratings_total=10,
                 formatted_address="123 Main St", google_maps_url="https://g/x")
    curr = _snap("h1", name="X", rating=4.5, user_ratings_total=13,
                 formatted_address="123 Main St", google_maps_url="https://g/x")
    signals = src.extract_signals(prev, curr)
    # 3 new reviews → 3 signals
    review_signals = [s for s in signals if s.signal_type_code == "gmb_review"]
    assert len(review_signals) == 3
    assert review_signals[0].extracted_facts["review_number"] == 11
    assert review_signals[-1].extracted_facts["review_number"] == 13


def test_gmb_review_count_decrease_emits_nothing():
    """If user_ratings_total appears to drop (review hidden / removed),
    we don't emit any review signal."""
    src = GMBListingSource()
    prev = _snap("h0", user_ratings_total=20)
    curr = _snap("h1", user_ratings_total=18)
    signals = src.extract_signals(prev, curr)
    review_signals = [s for s in signals if s.signal_type_code == "gmb_review"]
    assert review_signals == []


def test_gmb_rating_shift_under_noise_floor_ignored():
    """A 0.1 rating drift is noise (Google rounds at 1 decimal)."""
    src = GMBListingSource()
    prev = _snap("h0", rating=4.5, user_ratings_total=10)
    curr = _snap("h1", rating=4.6, user_ratings_total=10)
    signals = src.extract_signals(prev, curr)
    rating_signals = [s for s in signals if
                      s.extracted_facts.get("change_type") == "rating_shift"]
    assert rating_signals == []


def test_gmb_rating_shift_above_noise_floor_emits():
    """A 0.3 rating drop is meaningful (e.g., string of bad reviews)."""
    src = GMBListingSource()
    prev = _snap("h0", rating=4.5, user_ratings_total=20)
    curr = _snap("h1", rating=4.2, user_ratings_total=20)
    signals = src.extract_signals(prev, curr)
    rating_signals = [s for s in signals if
                      s.extracted_facts.get("change_type") == "rating_shift"]
    assert len(rating_signals) == 1
    assert rating_signals[0].extracted_facts["delta"] == -0.3


def test_gmb_address_change_emits_signal():
    src = GMBListingSource()
    prev = _snap("h0", formatted_address="123 Main St", user_ratings_total=10)
    curr = _snap("h1", formatted_address="456 New Ave", user_ratings_total=10)
    signals = src.extract_signals(prev, curr)
    addr = [s for s in signals if s.extracted_facts.get("change_type") == "address_changed"]
    assert len(addr) == 1
    assert addr[0].extracted_facts["prev_address"] == "123 Main St"
    assert addr[0].extracted_facts["curr_address"] == "456 New Ave"


def test_gmb_business_status_change_emits_signal():
    src = GMBListingSource()
    prev = _snap("h0", business_status="OPERATIONAL", user_ratings_total=10)
    curr = _snap("h1", business_status="CLOSED_PERMANENTLY", user_ratings_total=10)
    signals = src.extract_signals(prev, curr)
    biz = [s for s in signals if
           s.extracted_facts.get("change_type") == "business_status_changed"]
    assert len(biz) == 1
    assert biz[0].extracted_facts["curr"] == "CLOSED_PERMANENTLY"


def test_gmb_opening_hours_change_emits_signal():
    src = GMBListingSource()
    prev = _snap("h0", opening_hours_summary="Mon: 9-5 | Tue: 9-5",
                 user_ratings_total=10)
    curr = _snap("h1", opening_hours_summary="Mon: 9-7 | Tue: 9-7",
                 user_ratings_total=10)
    signals = src.extract_signals(prev, curr)
    hrs = [s for s in signals if
           s.extracted_facts.get("change_type") == "opening_hours_changed"]
    assert len(hrs) == 1


def test_gmb_no_change_emits_nothing():
    """Same snapshot data → no signals (catches false-positive class)."""
    src = GMBListingSource()
    data = dict(
        name="Acme",
        rating=4.5,
        user_ratings_total=10,
        formatted_address="123 Main St",
        business_status="OPERATIONAL",
        opening_hours_summary="Mon: 9-5",
    )
    prev = _snap("h", **data)
    curr = _snap("h", **data)
    signals = src.extract_signals(prev, curr)
    assert signals == []


# ── WebsiteHomepageSource.extract_signals ──────────────────────────────────

def test_website_first_poll_no_signal():
    src = WebsiteHomepageSource()
    snapshot = _snap("h0", text_length=5000, url="https://x.com")
    assert src.extract_signals(None, snapshot) == []


def test_website_change_below_noise_floor_ignored():
    """<5% change is treated as ticker / counter / quote rotation noise."""
    src = WebsiteHomepageSource()
    prev = _snap("h0", text_length=5000, url="https://x.com")
    curr = _snap("h1", text_length=5150, url="https://x.com")  # +3%
    assert src.extract_signals(prev, curr) == []


def test_website_change_above_noise_floor_emits():
    src = WebsiteHomepageSource()
    prev = _snap("h0", text_length=5000, url="https://x.com")
    curr = _snap("h1", text_length=6000, url="https://x.com")  # +20%
    signals = src.extract_signals(prev, curr)
    assert len(signals) == 1
    assert signals[0].signal_type_code == "website_change"
    assert signals[0].extracted_facts["delta_chars"] == 1000
    assert signals[0].extracted_facts["direction"] == "added"


def test_website_negative_change_above_noise_floor_emits():
    """Site got SMALLER (likely a redesign or content removal)."""
    src = WebsiteHomepageSource()
    prev = _snap("h0", text_length=10000, url="https://x.com")
    curr = _snap("h1", text_length=8000, url="https://x.com")  # -20%
    signals = src.extract_signals(prev, curr)
    assert len(signals) == 1
    assert signals[0].extracted_facts["delta_chars"] == -2000
    assert signals[0].extracted_facts["direction"] == "removed"


# ── WebsiteCareersSource.extract_signals ───────────────────────────────────

def test_careers_first_poll_no_signal():
    src = WebsiteCareersSource()
    snapshot = _snap("h0", role_keyword_count=5, url="https://x.com/careers")
    assert src.extract_signals(None, snapshot) == []


def test_careers_single_role_add_under_noise_floor():
    """Single role added isn't enough — common normal rotation."""
    src = WebsiteCareersSource()
    prev = _snap("h0", role_keyword_count=5, url="https://x.com/careers")
    curr = _snap("h1", role_keyword_count=6, url="https://x.com/careers")
    assert src.extract_signals(prev, curr) == []


def test_careers_multi_role_add_emits_hiring_signal():
    src = WebsiteCareersSource()
    prev = _snap("h0", role_keyword_count=5, url="https://x.com/careers")
    curr = _snap("h1", role_keyword_count=9, url="https://x.com/careers")  # +4
    signals = src.extract_signals(prev, curr)
    assert len(signals) == 1
    assert signals[0].signal_type_code == "hiring_signal"
    assert signals[0].extracted_facts["delta"] == 4


# ── Hiring stubs return empty ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hiring_indeed_stub_returns_no_signals():
    src = HiringIndeedSource()
    snapshot = await src.fetch("https://indeed.com/cmp/acme")
    assert snapshot.raw_data["stub"] is True
    assert src.extract_signals(None, snapshot) == []


@pytest.mark.asyncio
async def test_hiring_glassdoor_stub_returns_no_signals():
    src = HiringGlassdoorSource()
    snapshot = await src.fetch("https://glassdoor.com/acme")
    assert snapshot.raw_data["stub"] is True
    assert src.extract_signals(None, snapshot) == []
