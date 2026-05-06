"""
Netrows Email Finder Service

Wraps the Netrows /v1/email-finder/decision-maker endpoint, which is the
single most useful B2B-data API call for BMP's prospect base: it returns
a verified owner-tier email address for a given domain.

Hit rate against BMP's Vegas landscaper test set: 75% (3 of 4), all
returned with email_status='valid'. Cost: 10 credits per call.

Falls back to /v1/email-finder/by-domain (5 credits) when the decision-
maker endpoint returns 404 — picks up generic info@/office@ aliases that
Hunter would otherwise be needed for.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List
import httpx


BASE_URL = "https://api.netrows.com/v1"

# Categories ordered by likelihood of being the right person to email at a
# small home-services business. We try each in order until one succeeds.
DEFAULT_CATEGORIES = ("ceo", "operations", "marketing", "sales")


@dataclass
class NetrowsContact:
    email: str
    email_status: str = "unknown"  # 'valid', 'invalid', 'unknown'
    full_name: Optional[str] = None
    job_title: Optional[str] = None
    linkedin_url: Optional[str] = None
    category: Optional[str] = None  # which decision_maker_category matched
    source: str = "netrows_decision_maker"


@dataclass
class NetrowsResult:
    domain: str
    decision_makers: List[NetrowsContact] = field(default_factory=list)
    generic_emails: List[str] = field(default_factory=list)
    error: Optional[str] = None


def _clean_domain(domain: str) -> str:
    return (
        (domain or "")
        .replace("https://", "")
        .replace("http://", "")
        .split("/")[0]
        .replace("www.", "")
        .strip()
    )


async def find_decision_makers(
    domain: str,
    api_key: str,
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
) -> NetrowsResult:
    """
    Try each category in order; return all matches found.
    Each successful category costs 10 credits; categories that 404 also
    consume 10 credits (per Netrows docs: "Credits are deducted regardless
    of result"). Default tuple stops after the first match — call with a
    longer tuple to fetch multiple stakeholders.
    """
    domain = _clean_domain(domain)
    result = NetrowsResult(domain=domain)
    if not domain or not api_key:
        result.error = "missing domain or api_key"
        return result

    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient(timeout=20) as client:
        for category in categories:
            try:
                resp = await client.get(
                    f"{BASE_URL}/email-finder/decision-maker",
                    params={"domain": domain, "category": category},
                    headers=headers,
                )
            except httpx.HTTPError as e:
                result.error = f"network error: {e}"
                continue

            if resp.status_code == 200:
                data = resp.json()
                contact = NetrowsContact(
                    email=data.get("email", ""),
                    email_status=data.get("email_status", "unknown"),
                    full_name=data.get("person_full_name"),
                    job_title=data.get("person_job_title"),
                    linkedin_url=data.get("person_linkedin_url"),
                    category=data.get("decision_maker_category", category),
                )
                if contact.email:
                    result.decision_makers.append(contact)
                # First hit is usually the most relevant — break unless the caller
                # explicitly passed multiple categories. The default tuple has the
                # CEO/owner first, so breaking is the right behavior for SMB.
                break
            elif resp.status_code == 404:
                # No decision maker for this category — try next one
                continue
            elif resp.status_code in (401, 402, 429):
                result.error = f"netrows {resp.status_code}: {resp.text[:200]}"
                return result  # don't keep burning credits on auth/quota errors
            else:
                # Unexpected — record and try next
                result.error = f"netrows {resp.status_code}: {resp.text[:200]}"

        # If no decision-maker found, fall back to by-domain (5 credits)
        if not result.decision_makers and not result.error:
            try:
                resp = await client.get(
                    f"{BASE_URL}/email-finder/by-domain",
                    params={"domain": domain},
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    result.generic_emails = data.get("emails", []) or []
            except httpx.HTTPError:
                pass

    return result


async def find_email_by_name(
    first_name: str,
    last_name: str,
    domain: str,
    api_key: str,
) -> Optional[NetrowsContact]:
    """Find a verified email for a known person at a domain (5 credits)."""
    domain = _clean_domain(domain)
    if not (first_name and last_name and domain and api_key):
        return None

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.get(
                f"{BASE_URL}/email-finder/by-name",
                params={"first_name": first_name, "last_name": last_name, "domain": domain},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return None

    if resp.status_code != 200:
        return None
    data = resp.json()
    email = data.get("email")
    if not email:
        return None
    return NetrowsContact(
        email=email,
        email_status=data.get("email_status", "unknown"),
        full_name=f"{first_name} {last_name}",
        source="netrows_by_name",
    )


async def find_email_by_linkedin(
    linkedin_url: str,
    api_key: str,
) -> Optional[NetrowsContact]:
    """Find a verified business email by LinkedIn profile URL (5 credits)."""
    if not (linkedin_url and api_key):
        return None
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.get(
                f"{BASE_URL}/email-finder/by-linkedin",
                params={"linkedin_url": linkedin_url},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return None
    if resp.status_code != 200:
        return None
    data = resp.json()
    email = data.get("email")
    if not email:
        return None
    return NetrowsContact(
        email=email,
        email_status=data.get("email_status", "unknown"),
        full_name=data.get("person_full_name"),
        job_title=data.get("person_job_title"),
        linkedin_url=linkedin_url,
        source="netrows_by_linkedin",
    )


@dataclass
class ReverseLookupResult:
    full_name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    headline: Optional[str] = None
    linkedin_url: Optional[str] = None
    location: Optional[str] = None
    current_company: Optional[str] = None
    current_title: Optional[str] = None


async def reverse_email_lookup(email: str, api_key: str) -> Optional[ReverseLookupResult]:
    """
    Find a LinkedIn profile by email address (1 credit).
    Use to backfill name/title/LinkedIn when we have an email but no person info.
    """
    if not (email and api_key):
        return None
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.get(
                f"{BASE_URL}/people/reverse-lookup",
                params={"email": email},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return None
    if resp.status_code != 200:
        return None
    data = resp.json()
    # Response shapes vary; tolerate both {data: {...}} and direct
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
        data = data["data"]

    full_name = data.get("fullName") or data.get("full_name") or data.get("name")
    first = data.get("firstName") or data.get("first_name")
    last = data.get("lastName") or data.get("last_name")
    if full_name and not first and not last:
        parts = full_name.strip().split(maxsplit=1)
        first = parts[0]
        last = parts[1] if len(parts) > 1 else ""

    # Current position can live under positions[0] or current_position
    current = None
    if isinstance(data.get("positions"), list) and data["positions"]:
        current = data["positions"][0]
    elif isinstance(data.get("currentPosition"), dict):
        current = data["currentPosition"]

    return ReverseLookupResult(
        full_name=full_name,
        first_name=first,
        last_name=last,
        headline=data.get("headline"),
        linkedin_url=data.get("linkedinUrl") or data.get("url"),
        location=data.get("locationName") or data.get("location"),
        current_company=(current or {}).get("companyName") if current else None,
        current_title=(current or {}).get("title") if current else None,
    )


# ============================================================
# Google Maps reviews (1 credit) — owner replies are gold for personalization
# ============================================================

@dataclass
class MapsReview:
    author: Optional[str] = None
    rating: Optional[int] = None
    text: Optional[str] = None
    relative_time: Optional[str] = None
    owner_reply: Optional[str] = None  # text of business owner's reply
    owner_reply_time: Optional[str] = None


@dataclass
class MapsReviewsResult:
    place_id: Optional[str] = None
    name: Optional[str] = None
    reviews: List[MapsReview] = field(default_factory=list)


async def google_maps_reviews(
    feature_id_or_query: str,
    api_key: str,
) -> Optional[MapsReviewsResult]:
    """
    Fetch reviews for a Google Maps place. Accepts either a feature_id
    (e.g. '0x80c8...') or a search query that we resolve first.
    """
    if not (feature_id_or_query and api_key):
        return None
    headers = {"Authorization": f"Bearer {api_key}"}
    feature_id = feature_id_or_query
    place_name = None

    async with httpx.AsyncClient(timeout=20) as client:
        # If it doesn't look like a feature id, search first
        if not feature_id_or_query.startswith("0x"):
            try:
                sr = await client.get(
                    f"{BASE_URL}/google-maps/search",
                    params={"query": feature_id_or_query, "limit": 1},
                    headers=headers,
                )
                if sr.status_code == 200:
                    results = (sr.json() or {}).get("results", [])
                    if results:
                        feature_id = results[0].get("feature_id") or feature_id
                        place_name = results[0].get("name")
            except httpx.HTTPError:
                return None

        try:
            r = await client.get(
                f"{BASE_URL}/google-maps/reviews",
                params={"feature_id": feature_id},
                headers=headers,
            )
        except httpx.HTTPError:
            return None

    if r.status_code != 200:
        return None
    data = r.json() or {}
    raw_reviews = data.get("reviews") or data.get("data", {}).get("reviews") or []
    reviews: List[MapsReview] = []
    for rv in raw_reviews:
        owner_reply = None
        owner_reply_time = None
        # Owner reply lives under different keys depending on source shape
        reply = rv.get("response") or rv.get("owner_response") or rv.get("ownerReply")
        if isinstance(reply, dict):
            owner_reply = reply.get("text") or reply.get("body")
            owner_reply_time = reply.get("relative_time") or reply.get("time")
        elif isinstance(reply, str):
            owner_reply = reply

        reviews.append(MapsReview(
            author=rv.get("author_name") or rv.get("author"),
            rating=rv.get("rating"),
            text=rv.get("text") or rv.get("review_text") or rv.get("body"),
            relative_time=rv.get("relative_time") or rv.get("time_ago"),
            owner_reply=owner_reply,
            owner_reply_time=owner_reply_time,
        ))

    return MapsReviewsResult(place_id=feature_id, name=place_name or data.get("name"), reviews=reviews)


# ============================================================
# LinkedIn posts (1 credit) — recent posts for personalization context
# ============================================================

@dataclass
class LinkedInPost:
    text: Optional[str] = None
    posted_at: Optional[str] = None
    url: Optional[str] = None
    likes: Optional[int] = None
    comments: Optional[int] = None


async def linkedin_recent_posts(
    linkedin_url_or_username: str,
    api_key: str,
    limit: int = 5,
) -> List[LinkedInPost]:
    """
    Fetch recent LinkedIn posts for a person (1 credit per call).
    Use to give cold emails personalization context ("saw your post about X").
    """
    if not (linkedin_url_or_username and api_key):
        return []
    # Extract username from URL if given
    username = linkedin_url_or_username
    if "linkedin.com/in/" in username:
        username = username.split("linkedin.com/in/")[1].rstrip("/").split("/")[0]

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(
                f"{BASE_URL}/people/posts",
                params={"username": username, "limit": limit},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return []

    if r.status_code != 200:
        return []
    data = r.json() or {}
    raw = data.get("data", {}).get("items") if isinstance(data.get("data"), dict) else data.get("posts", [])
    raw = raw or []

    posts: List[LinkedInPost] = []
    for p in raw[:limit]:
        posts.append(LinkedInPost(
            text=p.get("text") or p.get("content") or p.get("commentary"),
            posted_at=p.get("postedAt") or p.get("posted_at") or p.get("relative_time") or p.get("date"),
            url=p.get("postUrl") or p.get("url") or p.get("permalink"),
            likes=p.get("likes") or p.get("reactions"),
            comments=p.get("comments") or p.get("comment_count"),
        ))
    return posts


# ============================================================
# Company enrichment by domain — employee count, industry, HQ
# ============================================================

@dataclass
class CompanyEnrichment:
    name: Optional[str] = None
    employee_count: Optional[int] = None
    industry: Optional[str] = None
    headquarters: Optional[str] = None
    linkedin_id: Optional[str] = None
    linkedin_username: Optional[str] = None
    founded: Optional[str] = None


async def enrich_company_by_domain(domain: str, api_key: str) -> Optional[CompanyEnrichment]:
    """
    Look up company info by domain via Netrows /companies/by-domain.
    Returns employee count, industry, HQ location. 1 credit per call.
    """
    domain = _clean_domain(domain)
    if not domain or not api_key:
        return None

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(
                f"{BASE_URL}/companies/by-domain",
                params={"domain": domain},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return None

    if r.status_code != 200:
        return None

    data = r.json() or {}
    # Handle nested data envelope
    company = data.get("data") or data

    return CompanyEnrichment(
        name=company.get("name"),
        employee_count=company.get("employeeCount"),
        industry=company.get("industry"),
        headquarters=company.get("headquarters"),
        linkedin_id=str(company.get("id", "")),
        linkedin_username=company.get("username"),
        founded=company.get("founded"),
    )
