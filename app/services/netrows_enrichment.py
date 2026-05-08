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


# ============================================================
# Tier 2 wrappers — qualifying + intent intel
# ============================================================
#
# All seven endpoints below tolerate Netrows response-shape variation
# (data wrapped in `data:`, alternate field names, items list vs bare
# array). On 4xx/5xx or transport error the wrappers return empty
# list / None rather than raising — callers should treat absence as
# "we don't know" not "the call failed".


def _unwrap(data: dict) -> dict | list:
    """Most Netrows endpoints wrap payload in {data: ...}; some don't."""
    if not isinstance(data, dict):
        return data
    inner = data.get("data")
    if inner is not None:
        return inner
    return data


# ----- /businesses/search (Yellow Pages) -----------------------------

@dataclass
class YPBusiness:
    """Yellow Pages SMB record. Useful as an alternative lead source
    when Yelp / Google Maps come up empty (rural areas, certain trades)."""
    name: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    categories: List[str] = field(default_factory=list)
    rating: Optional[float] = None
    review_count: Optional[int] = None
    yp_url: Optional[str] = None


async def yellow_pages_search(
    query: str,
    location: str,
    api_key: str,
    page: int = 1,
) -> List[YPBusiness]:
    """US Business search via Yellow Pages. ~30 results per page.
    `query` accepts category ('plumbers') or business name ('Smith Pools').
    `location` accepts 'City, ST' or ZIP."""
    if not (query and location and api_key):
        return []
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(
                f"{BASE_URL}/businesses/search",
                params={"query": query, "location": location, "page": page},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return []
    if r.status_code != 200:
        return []
    body = _unwrap(r.json() or {})
    items = body.get("items") if isinstance(body, dict) else body
    items = items or []
    out: List[YPBusiness] = []
    for b in items[:50]:
        if not isinstance(b, dict):
            continue
        addr = b.get("address") or {}
        if isinstance(addr, str):
            addr = {"street": addr}
        cats = b.get("categories") or b.get("category") or []
        if isinstance(cats, str):
            cats = [c.strip() for c in cats.split(",") if c.strip()]
        out.append(YPBusiness(
            name=b.get("name") or b.get("businessName"),
            phone=b.get("phone") or b.get("phoneNumber"),
            website=b.get("website") or b.get("url"),
            street=addr.get("street") or addr.get("street1") or b.get("street"),
            city=addr.get("city") or b.get("city"),
            state=addr.get("state") or b.get("state"),
            zip_code=addr.get("zip") or addr.get("postalCode") or b.get("zip"),
            categories=list(cats)[:5] if isinstance(cats, list) else [],
            rating=b.get("rating") or b.get("averageRating"),
            review_count=b.get("reviewCount") or b.get("reviewsCount"),
            yp_url=b.get("ypUrl") or b.get("profileUrl") or b.get("link"),
        ))
    return out


# ----- /yelp/business-search + business-details + business-reviews ---

@dataclass
class YelpBusiness:
    alias: Optional[str] = None  # the slug Yelp uses ('nobu-new-york')
    biz_id: Optional[str] = None
    name: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    yelp_url: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    price_range: Optional[str] = None  # '$', '$$', '$$$'
    categories: List[str] = field(default_factory=list)
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    hours_summary: Optional[str] = None
    photo_url: Optional[str] = None


@dataclass
class YelpReview:
    """A single Yelp review. The text + rating + date is the primary
    payload; for the highest-value cases (responses from the owner) we
    also surface owner_response — Steve's working theory is that owner-
    written replies on Yelp/Google Maps are gold for personalization
    ('I see how you handled that one-star — let's talk')."""
    rating: Optional[float] = None
    text: Optional[str] = None
    posted_at: Optional[str] = None
    reviewer_name: Optional[str] = None
    reviewer_profile_url: Optional[str] = None
    owner_response: Optional[str] = None
    owner_response_at: Optional[str] = None
    review_url: Optional[str] = None


def _parse_yelp_business(b: dict) -> YelpBusiness:
    cats = b.get("categories") or []
    if isinstance(cats, list):
        cats = [(c.get("title") if isinstance(c, dict) else c) for c in cats if c]
    elif isinstance(cats, str):
        cats = [c.strip() for c in cats.split(",") if c.strip()]
    addr = b.get("location") or b.get("address") or {}
    if isinstance(addr, str):
        addr = {"display_address": addr}
    return YelpBusiness(
        alias=b.get("alias") or b.get("slug"),
        biz_id=b.get("id") or b.get("business_id") or b.get("bizId"),
        name=b.get("name"),
        phone=b.get("phone") or b.get("displayPhone") or b.get("display_phone"),
        website=b.get("url") if "yelp.com" not in (b.get("url") or "") else (b.get("website") or None),
        yelp_url=b.get("url") if "yelp.com" in (b.get("url") or "") else b.get("yelpUrl"),
        rating=b.get("rating"),
        review_count=b.get("reviewCount") or b.get("review_count"),
        price_range=b.get("price"),
        categories=[c for c in cats if c][:8],
        address=addr.get("address1") or addr.get("display_address") or addr.get("street"),
        city=addr.get("city"),
        state=addr.get("state"),
        zip_code=addr.get("zipCode") or addr.get("zip_code") or addr.get("postalCode"),
        hours_summary=b.get("hoursSummary") or b.get("hours_summary"),
        photo_url=b.get("imageUrl") or b.get("image_url") or b.get("photo"),
    )


async def yelp_business_search(
    keyword: str,
    location: str,
    api_key: str,
    limit: int = 20,
) -> List[YelpBusiness]:
    if not (keyword and location and api_key):
        return []
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(
                f"{BASE_URL}/yelp/business-search",
                params={"keyword": keyword, "location": location, "limit": min(20, max(1, limit))},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return []
    if r.status_code != 200:
        return []
    body = _unwrap(r.json() or {})
    items = body.get("items") or body.get("businesses") if isinstance(body, dict) else body
    items = items or []
    return [_parse_yelp_business(b) for b in items if isinstance(b, dict)]


async def yelp_business_details(alias: str, api_key: str) -> Optional[YelpBusiness]:
    """Detail page for a Yelp business. `alias` is the slug
    ('nobu-new-york'); get it from yelp_business_search results
    or from the Yelp URL path."""
    if not (alias and api_key):
        return None
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(
                f"{BASE_URL}/yelp/business-details",
                params={"alias": alias},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return None
    if r.status_code != 200:
        return None
    body = _unwrap(r.json() or {})
    if isinstance(body, list):
        body = body[0] if body else {}
    return _parse_yelp_business(body) if isinstance(body, dict) else None


async def yelp_business_reviews(
    biz_id: str,
    alias: str,
    api_key: str,
    limit: int = 20,
) -> List[YelpReview]:
    """Reviews for a Yelp business. Both biz_id AND alias are required
    by Netrows. Owner responses (when present) are the most valuable
    field — surface them in the UI for personalization."""
    if not (biz_id and alias and api_key):
        return []
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(
                f"{BASE_URL}/yelp/business-reviews",
                params={"biz_id": biz_id, "alias": alias, "limit": min(20, max(1, limit))},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return []
    if r.status_code != 200:
        return []
    body = _unwrap(r.json() or {})
    items = body.get("items") or body.get("reviews") if isinstance(body, dict) else body
    items = items or []
    out: List[YelpReview] = []
    for v in items[:limit]:
        if not isinstance(v, dict):
            continue
        rev = v.get("user") or {}
        if isinstance(rev, str):
            rev = {"name": rev}
        owner = v.get("ownerResponse") or v.get("owner_response") or {}
        if isinstance(owner, str):
            owner = {"text": owner}
        out.append(YelpReview(
            rating=v.get("rating"),
            text=(v.get("text") or v.get("comment") or "")[:2000] or None,
            posted_at=v.get("timeCreated") or v.get("time_created") or v.get("date") or v.get("postedAt"),
            reviewer_name=rev.get("name") if isinstance(rev, dict) else None,
            reviewer_profile_url=rev.get("profileUrl") or rev.get("url") if isinstance(rev, dict) else None,
            owner_response=(owner.get("text") if isinstance(owner, dict) else None) or None,
            owner_response_at=(owner.get("postedAt") or owner.get("date") if isinstance(owner, dict) else None),
            review_url=v.get("url") or v.get("reviewUrl"),
        ))
    return out


# ----- /similarweb/website-overview ----------------------------------

@dataclass
class SimilarWebOverview:
    """Website traffic overview from SimilarWeb. The killer field for
    qualifying is `monthly_visits` — drop prospects with <100 visits/mo
    from cold-outreach cadences (likely abandoned site / dropshipper /
    parked domain)."""
    domain: Optional[str] = None
    global_rank: Optional[int] = None
    country_rank: Optional[int] = None
    category_rank: Optional[int] = None
    monthly_visits: Optional[int] = None
    bounce_rate: Optional[float] = None
    avg_visit_duration_seconds: Optional[float] = None
    pages_per_visit: Optional[float] = None
    top_country: Optional[str] = None
    top_country_share: Optional[float] = None
    traffic_sources: dict = field(default_factory=dict)  # direct/search/social/etc → fraction
    raw_payload: Optional[dict] = None


async def similarweb_website_overview(domain: str, api_key: str) -> Optional[SimilarWebOverview]:
    if not (domain and api_key):
        return None
    clean = (domain or "").replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")
    async with httpx.AsyncClient(timeout=25) as client:
        try:
            r = await client.get(
                f"{BASE_URL}/similarweb/website-overview",
                params={"domain": clean},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return None
    if r.status_code != 200:
        return None
    body = _unwrap(r.json() or {})
    if not isinstance(body, dict):
        return None
    engagement = body.get("engagement") or body.get("engagements") or {}
    if not isinstance(engagement, dict):
        engagement = {}
    sources = body.get("trafficSources") or body.get("traffic_sources") or {}
    if isinstance(sources, list):
        # Sometimes [{name,share}, ...] — flatten
        sources = {s.get("name", str(i)): s.get("share")
                   for i, s in enumerate(sources) if isinstance(s, dict)}
    top_countries = body.get("topCountries") or body.get("top_countries") or []
    top_country = None
    top_share = None
    if isinstance(top_countries, list) and top_countries:
        first = top_countries[0]
        if isinstance(first, dict):
            top_country = first.get("country") or first.get("code") or first.get("name")
            top_share = first.get("share") or first.get("visitShare")
    return SimilarWebOverview(
        domain=clean,
        global_rank=body.get("globalRank") or body.get("global_rank"),
        country_rank=body.get("countryRank") or body.get("country_rank"),
        category_rank=body.get("categoryRank") or body.get("category_rank"),
        monthly_visits=body.get("monthlyVisits") or body.get("monthly_visits") or body.get("visits"),
        bounce_rate=engagement.get("bounceRate") or engagement.get("bounce_rate"),
        avg_visit_duration_seconds=engagement.get("avgVisitDuration") or engagement.get("avg_visit_duration"),
        pages_per_visit=engagement.get("pagesPerVisit") or engagement.get("pages_per_visit"),
        top_country=top_country,
        top_country_share=top_share,
        traffic_sources=sources if isinstance(sources, dict) else {},
        raw_payload=body,
    )


# ----- /technographics/lookup (BuiltWith-equivalent) -----------------

@dataclass
class TechnographicsResult:
    """Detected technologies on a website. Categorized; useful both for
    qualifying ('uses Shopify' = e-commerce, different motion) and for
    objection prep ('they're on HubSpot — frame against that')."""
    url: Optional[str] = None
    technologies: List[dict] = field(default_factory=list)  # [{name, category, confidence}]
    categories: List[str] = field(default_factory=list)  # flattened category names
    cms: Optional[str] = None
    ecommerce: Optional[str] = None
    analytics: List[str] = field(default_factory=list)
    advertising: List[str] = field(default_factory=list)
    raw_payload: Optional[dict] = None


async def technographics_lookup(url: str, api_key: str) -> Optional[TechnographicsResult]:
    if not (url and api_key):
        return None
    async with httpx.AsyncClient(timeout=25) as client:
        try:
            r = await client.get(
                f"{BASE_URL}/technographics/lookup",
                params={"url": url},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return None
    if r.status_code != 200:
        return None
    body = _unwrap(r.json() or {})
    if not isinstance(body, dict):
        return None
    raw_techs = body.get("technologies") or body.get("techStack") or body.get("tech") or []
    if isinstance(raw_techs, dict):
        # Sometimes returned as {category: [tech, tech, ...]} — flatten
        flat = []
        for cat, lst in raw_techs.items():
            if isinstance(lst, list):
                for t in lst:
                    if isinstance(t, dict):
                        flat.append({**t, "category": t.get("category") or cat})
                    elif isinstance(t, str):
                        flat.append({"name": t, "category": cat})
        raw_techs = flat
    techs: List[dict] = []
    for t in raw_techs[:60] if isinstance(raw_techs, list) else []:
        if isinstance(t, str):
            techs.append({"name": t, "category": None, "confidence": None})
        elif isinstance(t, dict):
            techs.append({
                "name": t.get("name") or t.get("technology"),
                "category": t.get("category") or t.get("group"),
                "confidence": t.get("confidence") or t.get("score"),
            })
    categories = sorted({t["category"] for t in techs if t.get("category")})

    def _by_cat(*needles: str) -> List[str]:
        out: list[str] = []
        for t in techs:
            cat = (t.get("category") or "").lower()
            if any(n in cat for n in needles) and t.get("name"):
                out.append(t["name"])
        return out

    cms_list = _by_cat("cms", "content management")
    ecom_list = _by_cat("ecommerce", "e-commerce", "shop")
    return TechnographicsResult(
        url=url,
        technologies=techs,
        categories=categories,
        cms=cms_list[0] if cms_list else None,
        ecommerce=ecom_list[0] if ecom_list else None,
        analytics=_by_cat("analytics", "tag manag"),
        advertising=_by_cat("advertis", "ads", "tracking"),
        raw_payload=body,
    )


# ----- /indeed/job-search (by company name) -------------------------

@dataclass
class IndeedJob:
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    posted_at: Optional[str] = None
    job_url: Optional[str] = None
    salary: Optional[str] = None
    job_type: Optional[str] = None
    snippet: Optional[str] = None


async def indeed_jobs_for_company(
    company_name: str,
    api_key: str,
    location: Optional[str] = None,
    page: int = 1,
) -> List[IndeedJob]:
    """Indeed doesn't have a per-company search filter, so we use the
    company name as the freeform query. Caller should filter the
    returned list by exact company match — a search for 'Smith Pools'
    will surface jobs that just mention 'pools' too. Hiring activity =
    budget signal."""
    if not (company_name and api_key):
        return []
    params: dict = {"query": company_name.strip(), "page": page}
    if location:
        params["location"] = location
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(
                f"{BASE_URL}/indeed/job-search",
                params=params,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return []
    if r.status_code != 200:
        return []
    body = _unwrap(r.json() or {})
    items = body.get("items") or body.get("jobs") if isinstance(body, dict) else body
    items = items or []
    out: List[IndeedJob] = []
    norm_name = (company_name or "").strip().lower()
    for j in items[:30]:
        if not isinstance(j, dict):
            continue
        comp = (j.get("company") or j.get("companyName") or "").strip()
        # Conservative match: only return jobs whose company contains the
        # query name. Same defensive posture as enrich_company_by_domain
        # (avoid the Proficient Patios cross-record class of bugs).
        if norm_name and norm_name not in comp.lower():
            continue
        out.append(IndeedJob(
            title=j.get("title") or j.get("jobTitle"),
            company=comp or None,
            location=j.get("location") or j.get("locationName"),
            posted_at=j.get("postedAt") or j.get("posted_at") or j.get("date"),
            job_url=j.get("url") or j.get("jobUrl"),
            salary=j.get("salary") or j.get("salaryEstimate"),
            job_type=j.get("jobType") or j.get("type"),
            snippet=(j.get("snippet") or j.get("description") or "")[:300] or None,
        ))
    return out
