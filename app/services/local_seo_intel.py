"""
Local SEO Intelligence Service
Performs local SEO analysis on home service businesses.
Checks: schema markup, NAP consistency, GBP signals, review health,
page structure, service pages, citations, and AI search readiness.

Based on methodology from claude-seo local SEO analysis framework.
"""
from __future__ import annotations
import httpx
import json
import re
import time
from typing import Optional, List, Dict
from bs4 import BeautifulSoup
from dataclasses import dataclass, field


@dataclass
class LocalSEOAnalysis:
    url: str
    score: int = 0  # 0-100

    # Business detection
    business_type: str = "unknown"  # brick_and_mortar, sab, hybrid
    industry_vertical: str = "home_services"

    # GBP Signals
    has_gbp_embed: bool = False
    has_map_embed: bool = False

    # Reviews & Reputation
    review_count_on_page: Optional[int] = None
    star_rating_on_page: Optional[float] = None
    has_review_schema: bool = False
    has_testimonials: bool = False

    # NAP Consistency
    nap_found: bool = False
    business_name_on_page: str = ""
    phone_on_page: str = ""
    address_on_page: str = ""
    nap_in_schema: bool = False
    nap_in_footer: bool = False

    # Local On-Page SEO
    title_has_city: bool = False
    title_has_service: bool = False
    h1_has_local_intent: bool = False
    has_service_pages: bool = False
    service_page_count: int = 0
    has_click_to_call: bool = False
    has_contact_form: bool = False

    # Schema Markup
    has_local_business_schema: bool = False
    schema_type: str = ""
    schema_has_geo: bool = False
    schema_has_hours: bool = False
    schema_has_area_served: bool = False

    # Citations & Authority
    has_bbb_mention: bool = False
    has_chamber_mention: bool = False
    has_yelp_link: bool = False
    citation_signals: List[str] = field(default_factory=list)

    # AI Search Readiness
    robots_blocks_ai: bool = False
    has_llms_txt: bool = False
    ai_crawler_status: Dict[str, str] = field(default_factory=dict)

    # Problems & Opportunities
    findings: List[Dict] = field(default_factory=list)


async def analyze_local_seo(url: str, business_name: str = "", business_type_hint: str = "home_services") -> LocalSEOAnalysis:
    """
    Run a local SEO analysis on a business website.
    Returns scored analysis with specific findings for BDR talking points.
    """
    analysis = LocalSEOAnalysis(url=url)
    analysis.industry_vertical = business_type_hint

    if not url.startswith("http"):
        url = f"https://{url}"

    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; BackyardLeads/1.0)"},
        ) as client:
            response = await client.get(url)
            if response.status_code != 200:
                analysis.findings.append({
                    "category": "critical",
                    "issue": "Website unreachable",
                    "detail": f"Status {response.status_code}",
                    "talking_point": "Your website isn't loading properly — potential customers can't find you online."
                })
                return analysis

            html = response.text
            soup = BeautifulSoup(html, "lxml")

            # Run all checks
            _detect_business_type(soup, html, analysis)
            _check_schema_markup(soup, html, analysis)
            _check_nap(soup, html, analysis, business_name)
            _check_local_onpage(soup, html, analysis)
            _check_gbp_signals(soup, html, analysis)
            _check_review_signals(soup, html, analysis)
            _check_service_pages(soup, html, analysis)
            _check_citations(soup, html, analysis)
            _check_click_to_call(soup, html, analysis)

            # Check robots.txt and AI crawlers
            await _check_ai_readiness(client, url, analysis)

            # Calculate score
            _calculate_score(analysis)

    except Exception as e:
        analysis.findings.append({
            "category": "critical",
            "issue": "Analysis failed",
            "detail": str(e)[:200],
            "talking_point": "We had trouble analyzing your website — this could indicate technical issues."
        })

    return analysis


def _detect_business_type(soup: BeautifulSoup, html: str, analysis: LocalSEOAnalysis):
    """Detect if brick-and-mortar, service area business, or hybrid."""
    html_lower = html.lower()

    has_physical_address = bool(re.search(r'\d+\s+\w+\s+(st|street|ave|avenue|blvd|road|rd|drive|dr|lane|ln|way|court|ct)', html_lower))
    has_service_area = any(phrase in html_lower for phrase in [
        "serving", "service area", "we come to you", "mobile service",
        "on-site", "we serve", "areas we serve", "service areas"
    ])
    has_visit_us = any(phrase in html_lower for phrase in [
        "visit us", "come see us", "our showroom", "our location", "stop by"
    ])

    if has_physical_address and has_service_area:
        analysis.business_type = "hybrid"
    elif has_service_area and not has_visit_us:
        analysis.business_type = "service_area_business"
    elif has_physical_address:
        analysis.business_type = "brick_and_mortar"
    else:
        analysis.business_type = "service_area_business"  # default for home services


def _check_schema_markup(soup: BeautifulSoup, html: str, analysis: LocalSEOAnalysis):
    """Check for LocalBusiness structured data."""
    scripts = soup.find_all("script", type="application/ld+json")

    for script in scripts:
        try:
            data = json.loads(script.string)
            # Handle @graph arrays
            items = data if isinstance(data, list) else [data]
            if isinstance(data, dict) and "@graph" in data:
                items = data["@graph"]

            for item in items:
                item_type = item.get("@type", "")
                if isinstance(item_type, list):
                    item_type = item_type[0] if item_type else ""

                local_types = [
                    "LocalBusiness", "HomeAndConstructionBusiness",
                    "Plumber", "Electrician", "HVACBusiness",
                    "LandscapingBusiness", "RoofingContractor",
                    "GeneralContractor", "HousePainter",
                ]

                if any(lt.lower() in item_type.lower() for lt in local_types):
                    analysis.has_local_business_schema = True
                    analysis.schema_type = item_type

                    if item.get("geo"):
                        analysis.schema_has_geo = True
                    if item.get("openingHoursSpecification"):
                        analysis.schema_has_hours = True
                    if item.get("areaServed"):
                        analysis.schema_has_area_served = True
                    if item.get("address"):
                        analysis.nap_in_schema = True

                if item.get("aggregateRating"):
                    analysis.has_review_schema = True
                    rating = item["aggregateRating"]
                    analysis.star_rating_on_page = float(rating.get("ratingValue", 0))
                    analysis.review_count_on_page = int(rating.get("reviewCount", 0))

        except (json.JSONDecodeError, TypeError, ValueError):
            continue

    if not analysis.has_local_business_schema:
        analysis.findings.append({
            "category": "high",
            "issue": "No LocalBusiness schema markup",
            "detail": "Google can't properly understand your business type, hours, or service area",
            "talking_point": "Your website is missing structured data that tells Google you're a local business — this hurts your visibility in map results and AI search."
        })


def _check_nap(soup: BeautifulSoup, html: str, analysis: LocalSEOAnalysis, business_name: str):
    """Check NAP (Name, Address, Phone) consistency."""
    # Find phone numbers
    phone_pattern = r'[\(]?\d{3}[\)]?[-.\s]?\d{3}[-.\s]?\d{4}'
    phones = re.findall(phone_pattern, html)
    if phones:
        analysis.phone_on_page = phones[0]
        analysis.nap_found = True

    # Check footer for NAP
    footer = soup.find("footer")
    if footer:
        footer_text = footer.get_text()
        if re.search(phone_pattern, footer_text):
            analysis.nap_in_footer = True

    # Check for address
    address_pattern = r'\d+\s+[\w\s]+(?:st|street|ave|avenue|blvd|road|rd|drive|dr|lane|ln|way|court|ct)\.?\s*,?\s*[\w\s]+,?\s*[A-Z]{2}\s*\d{5}'
    addresses = re.findall(address_pattern, html, re.IGNORECASE)
    if addresses:
        analysis.address_on_page = addresses[0].strip()

    if not analysis.nap_found:
        analysis.findings.append({
            "category": "high",
            "issue": "No phone number visible on website",
            "detail": "NAP (Name, Address, Phone) not found in page content",
            "talking_point": "Your phone number isn't easily visible on your website — 76% of mobile 'near me' searches lead to a visit within 24 hours, but only if people can actually call you."
        })

    if not analysis.nap_in_footer:
        analysis.findings.append({
            "category": "medium",
            "issue": "NAP not in footer",
            "detail": "Phone/address should be in the footer on every page for consistency",
            "talking_point": "Your contact info isn't in your website footer — Google uses this as a consistency signal across your pages."
        })


def _check_local_onpage(soup: BeautifulSoup, html: str, analysis: LocalSEOAnalysis):
    """Check title tags and H1 for local intent."""
    title_tag = soup.find("title")
    title_text = title_tag.get_text(strip=True).lower() if title_tag else ""

    h1_tags = soup.find_all("h1")
    h1_text = " ".join(h.get_text(strip=True).lower() for h in h1_tags)

    # Common city/state patterns
    city_pattern = r'\b(austin|houston|dallas|phoenix|denver|atlanta|miami|tampa|orlando|charlotte|nashville|san antonio|jacksonville|seattle|portland|las vegas|raleigh|scottsdale|plano|frisco)\b'

    # Service keywords for home services
    service_pattern = r'\b(pool|landscap|lawn|outdoor kitchen|bbq|barbecue|deck|patio|fence|hardscape|irrigation|tree|concrete|mason|paving|remodel)\b'

    if re.search(city_pattern, title_text):
        analysis.title_has_city = True
    if re.search(service_pattern, title_text):
        analysis.title_has_service = True
    if re.search(city_pattern, h1_text) or re.search(service_pattern, h1_text):
        analysis.h1_has_local_intent = True

    if not analysis.title_has_city:
        analysis.findings.append({
            "category": "high",
            "issue": "No city/location in page title",
            "detail": f"Title: '{title_text[:60]}' — missing local keyword",
            "talking_point": "Your page title doesn't include your city — this is the #1 signal Google uses to show you in local search results. Your competitors who rank above you all have their city in their title."
        })

    if not analysis.title_has_service:
        analysis.findings.append({
            "category": "medium",
            "issue": "No service keyword in page title",
            "detail": "Title should include primary service (e.g., 'Pool Builder')",
            "talking_point": "Your title tag doesn't mention what you do — someone searching 'pool builder Austin' won't find you because Google doesn't know that's what you offer."
        })


def _check_gbp_signals(soup: BeautifulSoup, html: str, analysis: LocalSEOAnalysis):
    """Check for Google Business Profile integration."""
    html_lower = html.lower()

    # Map embeds
    iframes = soup.find_all("iframe")
    for iframe in iframes:
        src = iframe.get("src", "").lower()
        if "google.com/maps" in src or "maps.google" in src:
            analysis.has_map_embed = True
            analysis.has_gbp_embed = True
            break

    # GBP widget or review embed
    if "google.com/maps" in html_lower or "place_id" in html_lower:
        analysis.has_gbp_embed = True

    if not analysis.has_map_embed:
        analysis.findings.append({
            "category": "medium",
            "issue": "No Google Maps embed",
            "detail": "No map showing business location on website",
            "talking_point": "You don't have a Google Maps embed on your site — this reinforces your geographic relevance to Google and makes it easy for customers to get directions."
        })


def _check_review_signals(soup: BeautifulSoup, html: str, analysis: LocalSEOAnalysis):
    """Check for review/testimonial presence."""
    html_lower = html.lower()

    review_indicators = ["testimonial", "review", "what our customers say",
                         "client stories", "5 stars", "★", "star rating"]
    analysis.has_testimonials = any(ind in html_lower for ind in review_indicators)

    if not analysis.has_testimonials and not analysis.has_review_schema:
        analysis.findings.append({
            "category": "high",
            "issue": "No reviews or testimonials on website",
            "detail": "No review signals found in page content or schema",
            "talking_point": "There are no reviews or testimonials on your website — 93% of consumers say reviews impact their buying decisions, and Google uses review signals as 20% of local pack ranking."
        })


def _check_service_pages(soup: BeautifulSoup, html: str, analysis: LocalSEOAnalysis):
    """Check if the site has dedicated service pages."""
    links = soup.find_all("a", href=True)
    service_keywords = [
        "service", "pool", "landscap", "deck", "patio", "fence",
        "outdoor-kitchen", "bbq", "irrigation", "hardscape", "design",
        "maintenance", "installation", "repair", "renovation", "remodel",
        "cleaning", "lighting", "drainage", "retaining-wall", "concrete",
    ]

    service_pages = set()
    for link in links:
        href = link.get("href", "").lower()
        text = link.get_text(strip=True).lower()
        for kw in service_keywords:
            if kw in href or kw in text:
                service_pages.add(href)
                break

    analysis.service_page_count = len(service_pages)
    analysis.has_service_pages = len(service_pages) >= 3

    if not analysis.has_service_pages:
        analysis.findings.append({
            "category": "high",
            "issue": f"Only {analysis.service_page_count} service pages found",
            "detail": "Dedicated service pages are the #1 local organic ranking factor (Whitespark 2026)",
            "talking_point": "You only have {count} service pages — your competitors have individual pages for each service (pool design, pool maintenance, pool renovation) and they rank for each one. One page for everything means Google doesn't know what to rank you for.".format(count=analysis.service_page_count)
        })


def _check_citations(soup: BeautifulSoup, html: str, analysis: LocalSEOAnalysis):
    """Check for citation and authority signals."""
    html_lower = html.lower()

    if "bbb" in html_lower or "better business bureau" in html_lower:
        analysis.has_bbb_mention = True
        analysis.citation_signals.append("BBB")

    if "chamber of commerce" in html_lower:
        analysis.has_chamber_mention = True
        analysis.citation_signals.append("Chamber of Commerce")

    if "yelp" in html_lower:
        analysis.has_yelp_link = True
        analysis.citation_signals.append("Yelp")

    if "houzz" in html_lower:
        analysis.citation_signals.append("Houzz")

    if "angi" in html_lower or "angie" in html_lower:
        analysis.citation_signals.append("Angi")

    if "homeadvisor" in html_lower:
        analysis.citation_signals.append("HomeAdvisor")

    if "thumbtack" in html_lower:
        analysis.citation_signals.append("Thumbtack")

    if "nextdoor" in html_lower:
        analysis.citation_signals.append("Nextdoor")

    if len(analysis.citation_signals) < 2:
        analysis.findings.append({
            "category": "medium",
            "issue": "Weak citation/authority signals",
            "detail": f"Only found references to: {', '.join(analysis.citation_signals) or 'none'}",
            "talking_point": "Your website doesn't reference any industry directories (BBB, Houzz, Angi) — these are trust signals that Google and AI search engines use to verify your business is legitimate."
        })


def _check_click_to_call(soup: BeautifulSoup, html: str, analysis: LocalSEOAnalysis):
    """Check for tel: links (click to call)."""
    tel_links = soup.find_all("a", href=re.compile(r"^tel:"))
    analysis.has_click_to_call = len(tel_links) > 0

    if not analysis.has_click_to_call:
        analysis.findings.append({
            "category": "medium",
            "issue": "No click-to-call button",
            "detail": "No tel: links found — mobile users can't tap to call",
            "talking_point": "You don't have a click-to-call button — 76% of mobile 'near me' searches lead to a call, but your visitors have to manually dial your number."
        })


async def _check_ai_readiness(client: httpx.AsyncClient, url: str, analysis: LocalSEOAnalysis):
    """Check if AI crawlers are blocked in robots.txt."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    try:
        response = await client.get(robots_url, timeout=5)
        if response.status_code == 200:
            robots_text = response.text.lower()

            ai_crawlers = ["gptbot", "claudebot", "perplexitybot", "google-extended"]
            for crawler in ai_crawlers:
                if crawler in robots_text:
                    # Check if disallowed
                    if f"user-agent: {crawler}" in robots_text:
                        analysis.ai_crawler_status[crawler] = "referenced"
                        # Simple check for disallow after the user-agent
                        if "disallow: /" in robots_text:
                            analysis.robots_blocks_ai = True
                            analysis.ai_crawler_status[crawler] = "blocked"

            if analysis.robots_blocks_ai:
                analysis.findings.append({
                    "category": "medium",
                    "issue": "AI crawlers blocked in robots.txt",
                    "detail": f"Blocked: {[k for k,v in analysis.ai_crawler_status.items() if v == 'blocked']}",
                    "talking_point": "Your website is blocking AI search engines (ChatGPT, Claude, Perplexity) — 45% of consumers now use AI for local recommendations, and you're invisible to them."
                })
    except Exception:
        pass

    # Check for llms.txt
    try:
        llms_url = f"{parsed.scheme}://{parsed.netloc}/llms.txt"
        response = await client.get(llms_url, timeout=5)
        analysis.has_llms_txt = response.status_code == 200
    except Exception:
        pass


def _calculate_score(analysis: LocalSEOAnalysis):
    """Calculate overall local SEO score (0-100)."""
    score = 100

    # Deduct based on findings severity
    for finding in analysis.findings:
        if finding["category"] == "critical":
            score -= 20
        elif finding["category"] == "high":
            score -= 12
        elif finding["category"] == "medium":
            score -= 6
        elif finding["category"] == "low":
            score -= 3

    # Bonus points for positive signals
    if analysis.has_local_business_schema:
        score += 5
    if analysis.has_review_schema:
        score += 5
    if analysis.has_service_pages:
        score += 5
    if analysis.has_gbp_embed:
        score += 3
    if len(analysis.citation_signals) >= 3:
        score += 5

    analysis.score = max(0, min(100, score))


def local_seo_to_dict(analysis: LocalSEOAnalysis) -> dict:
    """Convert analysis to dictionary for storage/API response."""
    return {
        "url": analysis.url,
        "score": analysis.score,
        "business_type": analysis.business_type,
        "industry_vertical": analysis.industry_vertical,
        "has_local_business_schema": analysis.has_local_business_schema,
        "schema_type": analysis.schema_type,
        "has_map_embed": analysis.has_map_embed,
        "has_reviews": analysis.has_testimonials or analysis.has_review_schema,
        "review_count": analysis.review_count_on_page,
        "star_rating": analysis.star_rating_on_page,
        "nap_found": analysis.nap_found,
        "nap_in_footer": analysis.nap_in_footer,
        "title_has_city": analysis.title_has_city,
        "title_has_service": analysis.title_has_service,
        "service_page_count": analysis.service_page_count,
        "has_click_to_call": analysis.has_click_to_call,
        "citation_signals": analysis.citation_signals,
        "ai_crawler_status": analysis.ai_crawler_status,
        "findings": analysis.findings,
        "finding_count": len(analysis.findings),
    }
