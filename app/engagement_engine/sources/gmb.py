"""GMBListingSource — polls Google My Business via the Places API.

Strategy:
  1. Fetch Place Details with fields: name, rating, user_ratings_total,
     formatted_address, formatted_phone_number, website, opening_hours,
     types, business_status, reviews, photos
  2. Hash the relevant fields (NOT photo references — they rotate)
  3. Emit signals when:
     - user_ratings_total increased → 'gmb_review' (one signal per
       *delta* — if went from 5 → 8, emit 3 review signals not 1)
     - rating crossed a meaningful threshold (≥0.2 change) → 'gmb_listing_change'
     - formatted_address changed → 'gmb_listing_change' (relocation)
     - business_status changed (operational → permanently_closed etc.)
       → 'gmb_listing_change' with high relevance
     - opening_hours changed → 'gmb_listing_change'

We DO NOT store the raw review text in raw_data_json — Google ToS allows
display of reviews on Google surfaces, not cached aggregation by third
parties. We store review_count and rating only, with the review URL for
the BDR to visit if the AI scores the signal high.

API cost: Place Details is $17/1000 calls (Atmosphere SKU). At BMP scale
(2000 active observations, weekly poll = ~8500/month) that's ~$145/mo.
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime, timezone

import httpx

from app.engagement_engine.interfaces import (
    SignalSource,
    Snapshot,
    ExtractedSignal,
    SourceError,
)
from app.engagement_engine.sources.base import (
    hash_snapshot,
    DEFAULT_TIMEOUT_SECONDS,
    USER_AGENT,
)

log = logging.getLogger("engagement_engine.sources.gmb")


# Google Place Details fields we request. Keep this list minimal — extra
# fields cost extra (Basic + Contact + Atmosphere data SKU tiers).
PLACE_DETAILS_FIELDS = ",".join([
    "name",
    "rating",
    "user_ratings_total",
    "formatted_address",
    "formatted_phone_number",
    "website",
    "opening_hours",
    "types",
    "business_status",
    "url",  # Google Maps URL
])

PLACES_DETAILS_BASE = "https://maps.googleapis.com/maps/api/place/details/json"

# Minimum rating delta that counts as a meaningful change (Google rounds
# to 1 decimal; ±0.1 is one review's worth of movement on a small base).
RATING_NOISE_FLOOR = 0.2


class GMBListingSource:
    """Polls a Google My Business listing for a single contact's company.

    Expected observations.source_url format: the Google place_id, NOT a
    full URL. The signal_watcher uses observations.source_url verbatim;
    storing place_id there keeps the polling layer agnostic.
    """

    source_type_code: str = "gmb_listing"
    poll_interval_default_days: int = 7

    async def fetch(self, place_id: str) -> Snapshot:
        api_key = self._resolve_api_key()
        if not api_key:
            raise SourceError("GOOGLE_PLACES_API_KEY not configured")

        params = {
            "place_id": place_id,
            "fields": PLACE_DETAILS_FIELDS,
            "key": api_key,
        }
        try:
            async with httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT_SECONDS,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                r = await client.get(PLACES_DETAILS_BASE, params=params)
        except Exception as e:
            raise SourceError(
                f"GMB fetch failed for {place_id}: {type(e).__name__}: {e}"
            ) from e

        if r.status_code != 200:
            raise SourceError(
                f"GMB status {r.status_code} for {place_id}"
            )

        try:
            data = r.json()
        except json.JSONDecodeError as e:
            raise SourceError(f"GMB invalid JSON: {e}") from e

        api_status = data.get("status")
        if api_status not in ("OK", "ZERO_RESULTS"):
            # NOT_FOUND, INVALID_REQUEST, OVER_QUERY_LIMIT, etc.
            if api_status == "OVER_QUERY_LIMIT":
                raise SourceError(f"GMB rate limit (transient): {api_status}")
            raise SourceError(f"GMB API status {api_status} for {place_id}")

        result = (data or {}).get("result", {}) or {}

        # Extract the fields we care about — explicit subset, no raw payload.
        snapshot_data = {
            "place_id": place_id,
            "name": result.get("name"),
            "rating": result.get("rating"),
            "user_ratings_total": result.get("user_ratings_total"),
            "formatted_address": result.get("formatted_address"),
            "formatted_phone_number": result.get("formatted_phone_number"),
            "website": result.get("website"),
            "business_status": result.get("business_status"),
            "google_maps_url": result.get("url"),
            "types": result.get("types") or [],
            "opening_hours_summary": self._summarize_hours(
                result.get("opening_hours") or {}
            ),
        }

        content_hash = hash_snapshot(snapshot_data)
        return Snapshot(
            content_hash=content_hash,
            raw_data=snapshot_data,
            observed_at=datetime.now(timezone.utc),
        )

    def extract_signals(
        self,
        prev_snapshot: Snapshot | None,
        current_snapshot: Snapshot,
    ) -> list[ExtractedSignal]:
        if prev_snapshot is None:
            return []

        prev = prev_snapshot.raw_data or {}
        curr = current_snapshot.raw_data
        signals: list[ExtractedSignal] = []

        # 1. New reviews
        prev_total = int(prev.get("user_ratings_total") or 0)
        curr_total = int(curr.get("user_ratings_total") or 0)
        if curr_total > prev_total:
            # Emit one signal per new review so the relevance scorer can
            # rank each independently. Idempotency_key includes the review
            # number so we don't double-emit a single review if a transient
            # poll wobble surfaces it twice.
            for i in range(prev_total + 1, curr_total + 1):
                signals.append(ExtractedSignal(
                    signal_type_code="gmb_review",
                    extracted_facts={
                        "review_number": i,
                        "total_after": curr_total,
                        "current_rating": curr.get("rating"),
                        "place_id": curr.get("place_id"),
                    },
                    source_url=curr.get("google_maps_url"),
                ))

        # 2. Significant rating change (±0.2 or more)
        prev_rating = float(prev.get("rating") or 0)
        curr_rating = float(curr.get("rating") or 0)
        if prev_rating and curr_rating:
            delta = round(curr_rating - prev_rating, 2)
            if abs(delta) >= RATING_NOISE_FLOOR:
                signals.append(ExtractedSignal(
                    signal_type_code="gmb_listing_change",
                    extracted_facts={
                        "change_type": "rating_shift",
                        "prev_rating": prev_rating,
                        "curr_rating": curr_rating,
                        "delta": delta,
                    },
                    source_url=curr.get("google_maps_url"),
                ))

        # 3. Address change (relocation — meaningful enough to interrupt
        # cadence)
        if prev.get("formatted_address") and curr.get("formatted_address"):
            if prev["formatted_address"] != curr["formatted_address"]:
                signals.append(ExtractedSignal(
                    signal_type_code="gmb_listing_change",
                    extracted_facts={
                        "change_type": "address_changed",
                        "prev_address": prev["formatted_address"],
                        "curr_address": curr["formatted_address"],
                    },
                    source_url=curr.get("google_maps_url"),
                ))

        # 4. Business status change (operational → closed / suspended)
        if prev.get("business_status") and curr.get("business_status"):
            if prev["business_status"] != curr["business_status"]:
                signals.append(ExtractedSignal(
                    signal_type_code="gmb_listing_change",
                    extracted_facts={
                        "change_type": "business_status_changed",
                        "prev": prev["business_status"],
                        "curr": curr["business_status"],
                    },
                    source_url=curr.get("google_maps_url"),
                ))

        # 5. Opening hours change
        if prev.get("opening_hours_summary") != curr.get("opening_hours_summary"):
            if prev.get("opening_hours_summary") and curr.get("opening_hours_summary"):
                signals.append(ExtractedSignal(
                    signal_type_code="gmb_listing_change",
                    extracted_facts={
                        "change_type": "opening_hours_changed",
                        "prev_summary": prev["opening_hours_summary"],
                        "curr_summary": curr["opening_hours_summary"],
                    },
                    source_url=curr.get("google_maps_url"),
                ))

        return signals

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_api_key() -> str | None:
        """Resolve API key from env. Per-tenant keys live in tenant_secrets
        and are resolved by the signal_watcher before calling fetch (it
        passes the resolved key through context). For Phase 3 minimum we
        fall back to the global env."""
        return (
            os.environ.get("GOOGLE_PLACES_API_KEY")
            or os.environ.get("GOOGLE_MAPS_API_KEY")
        )

    @staticmethod
    def _summarize_hours(hours: dict) -> str | None:
        """Compress opening_hours dict to a stable string. Google returns
        weekday_text as a list; joining gives a stable canonical form."""
        if not hours:
            return None
        weekday_text = hours.get("weekday_text") or []
        if weekday_text:
            return " | ".join(weekday_text)
        # Fallback: stringified open_now (less stable; mostly for tests)
        return f"open_now:{hours.get('open_now')}"
