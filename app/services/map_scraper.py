"""
Map Scraper Service
Finds businesses via Google Maps/Places API based on keyword + location.
Returns structured business data (name, phone, website, address, ratings).
"""
from __future__ import annotations
import httpx
from dataclasses import dataclass
from typing import Optional


@dataclass
class BusinessResult:
    name: str
    phone: Optional[str]
    website: Optional[str]
    address: Optional[str]
    city: Optional[str]
    state: Optional[str]
    rating: Optional[float]
    review_count: Optional[int]
    business_type: Optional[str]
    place_id: Optional[str]


async def search_businesses(
    keyword: str,
    location: str,
    api_key: str,
    max_results: int = 40,
) -> list[BusinessResult]:
    """
    Search Google Places API for businesses matching keyword + location.
    Example: keyword="pool builders", location="Austin, TX"
    """
    results = []
    base_url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    query = f"{keyword} in {location}"

    async with httpx.AsyncClient(timeout=30) as client:
        params = {"query": query, "key": api_key}
        next_page_token = None

        while len(results) < max_results:
            if next_page_token:
                params["pagetoken"] = next_page_token

            response = await client.get(base_url, params=params)
            data = response.json()

            if data.get("status") != "OK":
                break

            for place in data.get("results", []):
                if len(results) >= max_results:
                    break

                # Get details for phone and website
                detail = await _get_place_details(
                    client, place["place_id"], api_key
                )

                address = place.get("formatted_address", "")
                city, state = _parse_city_state(address)

                results.append(
                    BusinessResult(
                        name=place.get("name", ""),
                        phone=detail.get("phone"),
                        website=detail.get("website"),
                        address=address,
                        city=city,
                        state=state,
                        rating=place.get("rating"),
                        review_count=place.get("user_ratings_total"),
                        business_type=keyword,
                        place_id=place.get("place_id"),
                    )
                )

            next_page_token = data.get("next_page_token")
            if not next_page_token:
                break

            # Google requires a short delay before using next_page_token
            import asyncio
            await asyncio.sleep(2)

    return results


async def _get_place_details(
    client: httpx.AsyncClient, place_id: str, api_key: str
) -> dict:
    """Get phone number and website from Place Details API."""
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": "formatted_phone_number,website",
        "key": api_key,
    }
    try:
        response = await client.get(url, params=params)
        data = response.json()
        result = data.get("result", {})
        return {
            "phone": result.get("formatted_phone_number"),
            "website": result.get("website"),
        }
    except Exception:
        return {"phone": None, "website": None}


def _parse_city_state(address: str) -> tuple:
    """Extract city and state from a formatted address string."""
    parts = [p.strip() for p in address.split(",")]
    if len(parts) >= 3:
        city = parts[-3]
        state_zip = parts[-2].strip().split(" ")
        state = state_zip[0] if state_zip else None
        return city, state
    return None, None
