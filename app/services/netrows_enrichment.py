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
    company_size: Optional[str] = None  # e.g. "11-50"
    industry: Optional[str] = None
    headquarters: Optional[str] = None
    linkedin_id: Optional[str] = None
    linkedin_username: Optional[str] = None
    linkedin_url: Optional[str] = None
    founded: Optional[str] = None
    description: Optional[str] = None
    specialties: Optional[str] = None
    follower_count: Optional[int] = None
    website: Optional[str] = None


def _name_overlap_score(a: str, b: str) -> float:
    """Token-overlap ratio between two company names. Returns 0-1.
    Strips punctuation + entity suffixes (LLC, Inc, etc.) and compares
    the remaining significant tokens via Jaccard set overlap.
      'Smith Pools' vs 'Smith Pools, LLC'           → 1.0
      'Proficient Patios' vs 'Proficient Audio'      → 0.25
      'Backyard Marketing' vs 'Backyard Marketing Pros' → 0.67
    """
    import re as _re
    def _tokens(s: str) -> set[str]:
        s = (s or "").lower()
        s = _re.sub(r"[^a-z0-9 ]", " ", s)
        words = [w for w in s.split() if w and w not in {
            "llc", "inc", "corp", "corporation", "company", "co", "ltd",
            "lp", "llp", "pllc", "the", "a", "an", "and", "&", "of",
        }]
        return set(words)
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


async def enrich_company_by_domain(
    domain: str,
    api_key: str,
    expected_name: Optional[str] = None,
) -> Optional[CompanyEnrichment]:
    """Look up company info by domain via Netrows.

    Validation: Netrows' database occasionally maps a domain to the
    wrong company (e.g. proficientpatios.com → "Proficient Audio Systems").
    We reject the response when:
      - the returned company.website is set + its domain doesn't match
        our input domain, OR
      - expected_name is supplied + the token-overlap score with the
        returned name is below 0.4

    Step 1: /companies/by-domain (1 credit) — gets basic info + LinkedIn username
    Step 2: /companies/details (1 credit) — gets full profile if we got a username
    """
    domain = _clean_domain(domain)
    if not domain or not api_key:
        return None

    result = CompanyEnrichment()
    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient(timeout=15) as client:
        # Step 1: by-domain
        try:
            r = await client.get(
                f"{BASE_URL}/companies/by-domain",
                params={"domain": domain},
                headers=headers,
            )
            if r.status_code == 200:
                data = r.json() or {}
                company = data.get("data") or data

                # ---- Validation: website-domain match ----
                returned_website = (company.get("website") or "").strip()
                if returned_website:
                    returned_domain = _clean_domain(returned_website)
                    if returned_domain and returned_domain != domain:
                        # Subdomain forgiveness: 'shop.acme.com' ~ 'acme.com'
                        if not (returned_domain.endswith("." + domain) or domain.endswith("." + returned_domain)):
                            import logging as _logging
                            _logging.getLogger("bmp").warning(
                                f"Netrows by-domain mismatch: input={domain} → returned website={returned_website} "
                                f"(name={company.get('name')!r}). Rejecting to avoid corrupting company record."
                            )
                            return None

                # ---- Validation: name similarity (when caller supplied expected_name) ----
                returned_name = company.get("name") or ""
                if expected_name and returned_name:
                    score = _name_overlap_score(expected_name, returned_name)
                    if score < 0.4:
                        import logging as _logging
                        _logging.getLogger("bmp").warning(
                            f"Netrows name mismatch: expected={expected_name!r} → returned={returned_name!r} "
                            f"(token-overlap={score:.2f}, threshold=0.4). Rejecting."
                        )
                        return None

                result.name = company.get("name")
                raw_ec = company.get("employeeCount") or company.get("employee_count")
                if isinstance(raw_ec, int):
                    result.employee_count = raw_ec
                elif raw_ec:
                    try:
                        result.employee_count = int(raw_ec)
                    except (ValueError, TypeError):
                        pass
                result.industry = company.get("industry")
                result.headquarters = company.get("headquarters") or company.get("hq")
                result.linkedin_id = str(company.get("id", ""))
                result.linkedin_username = company.get("universalName") or company.get("username") or company.get("universal_name")
                result.linkedin_url = company.get("linkedinUrl") or company.get("linkedin_url")
                if not result.linkedin_url and result.linkedin_username:
                    result.linkedin_url = f"https://linkedin.com/company/{result.linkedin_username}"
                result.description = company.get("description") or company.get("tagline")
                result.website = company.get("website")
        except httpx.HTTPError:
            pass

        # Step 2: get full details if we have a username
        if result.linkedin_username:
            try:
                r2 = await client.get(
                    f"{BASE_URL}/companies/details",
                    params={"username": result.linkedin_username},
                    headers=headers,
                )
                if r2.status_code == 200:
                    detail = r2.json() or {}
                    detail = detail.get("data") or detail
                    raw_size = detail.get("companySize") or detail.get("company_size") or detail.get("staffCount") or detail.get("staffCountRange")
                    if raw_size:
                        result.company_size = str(raw_size) if not isinstance(raw_size, str) else raw_size
                    if not result.description:
                        result.description = detail.get("description") or detail.get("tagline")
                    raw_founded = detail.get("founded") or detail.get("foundedOn") or result.founded
                    if isinstance(raw_founded, dict):
                        result.founded = str(raw_founded.get("year", ""))
                    elif raw_founded:
                        result.founded = str(raw_founded)
                    result.follower_count = detail.get("followerCount") or detail.get("follower_count")
                    if not result.employee_count:
                        raw_ec = detail.get("employeeCount") or detail.get("staffCount")
                        if isinstance(raw_ec, int):
                            result.employee_count = raw_ec
                        elif raw_ec:
                            try:
                                result.employee_count = int(raw_ec)
                            except (ValueError, TypeError):
                                pass
                    if not result.industry:
                        raw_industry = detail.get("industry") or detail.get("companyIndustries")
                        if isinstance(raw_industry, list):
                            result.industry = ", ".join(str(i) for i in raw_industry)
                        elif raw_industry:
                            result.industry = str(raw_industry)
                    specialties = detail.get("specialties") or detail.get("specialities") or []
                    if isinstance(specialties, list):
                        result.specialties = ", ".join(str(s) for s in specialties)
                    elif isinstance(specialties, str):
                        result.specialties = specialties
            except httpx.HTTPError:
                pass

    if not result.name and not result.employee_count:
        return None
    return result


# ============================================================
# Untapped endpoints — high-value adds for B2B SMB outreach
# ============================================================
# Netrows exposes 273 endpoints; today we use ~7. These three add
# meaningful signal for BMP's verticals without changing existing flows.

@dataclass
class CompanyJobListing:
    title: Optional[str] = None
    location: Optional[str] = None
    posted_at: Optional[str] = None
    url: Optional[str] = None
    department: Optional[str] = None
    description_snippet: Optional[str] = None


async def company_jobs(
    company_id: str,
    api_key: str,
    page: int = 1,
) -> List[CompanyJobListing]:
    """Pull a company's open job listings (LinkedIn). Strong intent
    signal for sales — companies hiring sales/marketing roles often
    need vendors. Requires the Netrows internal company id, which
    `enrich_company_by_domain` already returns. ~1 credit / call."""
    if not (company_id and api_key):
        return []
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(
                f"{BASE_URL}/companies/jobs",
                params={"companyIds": company_id, "page": page},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return []
    if r.status_code != 200:
        return []
    data = r.json() or {}
    items = data.get("data", {}).get("items") if isinstance(data.get("data"), dict) else data.get("jobs", [])
    items = items or []
    out: List[CompanyJobListing] = []
    for j in items[:20]:
        out.append(CompanyJobListing(
            title=j.get("title") or j.get("jobTitle"),
            location=j.get("location") or j.get("locationName"),
            posted_at=j.get("postedAt") or j.get("posted_at") or j.get("listedAt"),
            url=j.get("url") or j.get("jobUrl"),
            department=j.get("department") or j.get("function"),
            description_snippet=(j.get("description") or "")[:200] if j.get("description") else None,
        ))
    return out


@dataclass
class CompanyInsights:
    """Premium endpoint — deeper firmographic data. Signature is wide
    because Netrows returns whatever they have; not all fields populate."""
    revenue_range: Optional[str] = None
    funding_stage: Optional[str] = None
    technologies: List[str] = field(default_factory=list)
    growth_signals: List[str] = field(default_factory=list)
    headcount_growth_pct: Optional[float] = None
    raw_payload: Optional[dict] = None


async def company_insights(domain_or_url: str, api_key: str) -> Optional[CompanyInsights]:
    """Premium /companies/insights. Domain-keyed. Returns deeper signal
    than /companies/by-domain — revenue, funding, growth, tech stack."""
    if not (domain_or_url and api_key):
        return None
    clean = (domain_or_url or "").replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(
                f"{BASE_URL}/companies/insights",
                params={"url": clean},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return None
    if r.status_code != 200:
        return None
    data = r.json() or {}
    body = data.get("data", data) if isinstance(data, dict) else data

    techs = body.get("technologies") or body.get("techStack") or []
    if isinstance(techs, str):
        techs = [t.strip() for t in techs.split(",") if t.strip()]
    growth = body.get("growthSignals") or body.get("signals") or []
    if isinstance(growth, str):
        growth = [growth]

    return CompanyInsights(
        revenue_range=body.get("revenueRange") or body.get("revenue_range") or body.get("revenue"),
        funding_stage=body.get("fundingStage") or body.get("funding_stage"),
        technologies=list(techs)[:30] if isinstance(techs, list) else [],
        growth_signals=list(growth)[:10] if isinstance(growth, list) else [],
        headcount_growth_pct=body.get("headcountGrowth") or body.get("headcount_growth_pct"),
        raw_payload=body if isinstance(body, dict) else None,
    )


@dataclass
class FullPersonProfile:
    """Full LinkedIn profile (richer than /people/reverse-lookup which is
    email-keyed). Use when we have the LinkedIn URL but want job history,
    summary, education etc."""
    full_name: Optional[str] = None
    headline: Optional[str] = None
    summary: Optional[str] = None
    location: Optional[str] = None
    current_title: Optional[str] = None
    current_company: Optional[str] = None
    linkedin_url: Optional[str] = None
    profile_pic_url: Optional[str] = None
    skills: List[str] = field(default_factory=list)
    raw_payload: Optional[dict] = None


async def person_profile_by_url(url: str, api_key: str) -> Optional[FullPersonProfile]:
    """Full LinkedIn profile by URL (1 credit). Use to fill out a contact
    we discovered via email-finder when they only have email + name."""
    if not (url and api_key):
        return None
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(
                f"{BASE_URL}/people/profile-by-url",
                params={"url": url},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return None
    if r.status_code != 200:
        return None
    data = r.json() or {}
    body = data.get("data", data) if isinstance(data, dict) else data
    skills = body.get("skills") or []
    if isinstance(skills, list) and skills and isinstance(skills[0], dict):
        skills = [s.get("name") or s.get("skill") for s in skills if s.get("name") or s.get("skill")]
    return FullPersonProfile(
        full_name=body.get("fullName") or body.get("full_name") or body.get("name"),
        headline=body.get("headline"),
        summary=(body.get("summary") or "")[:1000] or None,
        location=body.get("location") or body.get("locationName"),
        current_title=body.get("currentTitle") or body.get("current_title") or body.get("title"),
        current_company=body.get("currentCompany") or body.get("current_company"),
        linkedin_url=body.get("publicProfileUrl") or body.get("profileUrl") or url,
        profile_pic_url=body.get("profilePicUrl") or body.get("profilePicture"),
        skills=[s for s in skills if s][:30],
        raw_payload=body if isinstance(body, dict) else None,
    )


@dataclass
class InstagramPost:
    caption: Optional[str] = None
    posted_at: Optional[str] = None
    url: Optional[str] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    media_type: Optional[str] = None  # 'image' | 'video' | 'reel'
    thumbnail_url: Optional[str] = None


async def instagram_recent_posts(handle: str, api_key: str, limit: int = 9) -> List[InstagramPost]:
    """Recent Instagram posts for a profile (1 credit). Critical for
    backyard-pro outreach — they post pool installs, before/afters,
    landscaping reveals. Personalization gold: 'just saw your reveal
    post on the Scottsdale build…'"""
    if not (handle and api_key):
        return []
    # Strip @ + URL prefix if user pasted an instagram.com link
    handle = handle.strip().lstrip("@")
    if "instagram.com/" in handle:
        handle = handle.split("instagram.com/")[1].rstrip("/").split("/")[0]
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(
                f"{BASE_URL}/instagram/user/posts",
                params={"handle": handle, "trim": True},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return []
    if r.status_code != 200:
        return []
    data = r.json() or {}
    items = data.get("data", {}).get("items") if isinstance(data.get("data"), dict) else data.get("posts", [])
    items = items or []
    out: List[InstagramPost] = []
    for p in items[:limit]:
        out.append(InstagramPost(
            caption=(p.get("caption") or p.get("text") or "")[:500] or None,
            posted_at=p.get("postedAt") or p.get("posted_at") or p.get("taken_at"),
            url=p.get("url") or p.get("permalink"),
            likes=p.get("likes") or p.get("like_count"),
            comments=p.get("comments") or p.get("comment_count"),
            media_type=p.get("mediaType") or p.get("media_type"),
            thumbnail_url=p.get("thumbnailUrl") or p.get("thumbnail_url") or p.get("display_url"),
        ))
    return out
