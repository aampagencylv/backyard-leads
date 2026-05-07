"""
DataForSEO API integration for comprehensive audit data.
Provides real keyword rankings, domain authority, on-page technical audit,
SERP competitor data, and backlink profiles.

Auth: HTTP Basic (login:password base64 encoded).
Docs: https://docs.dataforseo.com/
"""
from __future__ import annotations
import base64
import httpx
from typing import Optional, List, Dict
from dataclasses import dataclass, field


BASE_URL = "https://api.dataforseo.com/v3"


def _auth_header(login: str, password: str) -> dict:
    creds = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}


# ============================================================
# On-Page API — deep technical audit
# ============================================================

@dataclass
class OnPageResult:
    """Results from DataForSEO On-Page instant analysis."""
    title: str = ""
    description: str = ""
    h1: str = ""
    canonical: str = ""
    word_count: int = 0
    internal_links: int = 0
    external_links: int = 0
    images_count: int = 0
    images_without_alt: int = 0
    has_robots_txt: bool = False
    has_sitemap: bool = False
    is_https: bool = False
    page_size_kb: float = 0
    load_time_ms: int = 0
    # Core Web Vitals (from Lighthouse/CrUX if available)
    cumulative_layout_shift: Optional[float] = None
    largest_contentful_paint: Optional[float] = None
    total_dom_size: int = 0
    # Structured data
    schema_types: List[str] = field(default_factory=list)
    # Mobile
    is_mobile_friendly: Optional[bool] = None
    # Errors
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


async def onpage_instant(url: str, login: str, password: str) -> Optional[OnPageResult]:
    """
    Run DataForSEO On-Page instant analysis on a single URL.
    Cost: ~$0.01-0.02 per call.
    """
    result = OnPageResult()

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(
                f"{BASE_URL}/on_page/instant_pages",
                headers=_auth_header(login, password),
                json=[{"url": url, "enable_javascript_rendering": True}],
            )
            if r.status_code != 200:
                return None

            data = r.json()
            tasks = data.get("tasks", [])
            if not tasks or not tasks[0].get("result"):
                return None

            items = tasks[0]["result"][0].get("items", [])
            if not items:
                return None

            page = items[0]
            meta = page.get("meta", {})
            result.title = meta.get("title", "")
            result.description = meta.get("description", "")
            result.h1 = meta.get("htags", {}).get("h1", [""])[0] if meta.get("htags", {}).get("h1") else ""
            result.canonical = meta.get("canonical", "")
            result.word_count = meta.get("content", {}).get("plain_text_word_count", 0) if isinstance(meta.get("content"), dict) else 0

            result.internal_links = page.get("internal_links_count", 0)
            result.external_links = page.get("external_links_count", 0)
            result.images_count = page.get("images_count", 0)
            result.images_without_alt = page.get("images_without_alt_count", 0)
            result.is_https = page.get("is_https", False)
            result.page_size_kb = round((page.get("size", 0) or 0) / 1024, 1)
            result.load_time_ms = page.get("fetch_timing", {}).get("duration_time", 0) if isinstance(page.get("fetch_timing"), dict) else 0
            result.total_dom_size = page.get("total_dom_size", 0)

            # Schema types
            schemas = page.get("schema_types", []) or []
            result.schema_types = schemas if isinstance(schemas, list) else []

            # Mobile
            result.is_mobile_friendly = page.get("is_mobile_friendly")

            # Checks / errors
            checks = page.get("checks", {}) or {}
            for check_name, check_val in checks.items():
                if check_val is True and check_name.startswith("no_"):
                    result.warnings.append(check_name.replace("_", " "))

        except Exception:
            return None

    return result


# ============================================================
# SERP API — who ranks for a keyword
# ============================================================

@dataclass
class SERPCompetitor:
    rank: int = 0
    url: str = ""
    title: str = ""
    domain: str = ""
    description: str = ""
    is_featured_snippet: bool = False


@dataclass
class SERPResult:
    keyword: str = ""
    location: str = ""
    total_results: int = 0
    competitors: List[SERPCompetitor] = field(default_factory=list)
    has_ai_overview: bool = False
    has_local_pack: bool = False
    has_featured_snippet: bool = False


async def serp_check(keyword: str, location: str, login: str, password: str) -> Optional[SERPResult]:
    """
    Check Google SERP for a keyword + location.
    Returns top 10 organic results + SERP feature detection.
    Cost: ~$0.003 per call.
    """
    result = SERPResult(keyword=keyword, location=location)

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(
                f"{BASE_URL}/serp/google/organic/live/regular",
                headers=_auth_header(login, password),
                json=[{
                    "keyword": keyword,
                    "location_name": location,
                    "language_code": "en",
                    "depth": 10,
                }],
            )
            if r.status_code != 200:
                return None

            data = r.json()
            tasks = data.get("tasks", [])
            if not tasks or not tasks[0].get("result"):
                return None

            serp = tasks[0]["result"][0]
            result.total_results = serp.get("se_results_count", 0)

            for item in serp.get("items", []):
                item_type = item.get("type", "")

                if item_type == "organic":
                    result.competitors.append(SERPCompetitor(
                        rank=item.get("rank_absolute", 0),
                        url=item.get("url", ""),
                        title=item.get("title", ""),
                        domain=item.get("domain", ""),
                        description=item.get("description", ""),
                    ))

                elif item_type == "featured_snippet":
                    result.has_featured_snippet = True
                    result.competitors.insert(0, SERPCompetitor(
                        rank=0,
                        url=item.get("url", ""),
                        title=item.get("title", ""),
                        domain=item.get("domain", ""),
                        description=item.get("description", ""),
                        is_featured_snippet=True,
                    ))

                elif item_type == "ai_overview":
                    result.has_ai_overview = True

                elif item_type == "local_pack":
                    result.has_local_pack = True

        except Exception:
            return None

    return result


# ============================================================
# Domain Ranked Keywords — what keywords a domain ranks for
# ============================================================

@dataclass
class RankedKeyword:
    keyword: str = ""
    position: int = 0
    search_volume: int = 0
    url: str = ""


@dataclass
class DomainKeywordsResult:
    domain: str = ""
    total_keywords: int = 0
    organic_traffic: int = 0
    top_keywords: List[RankedKeyword] = field(default_factory=list)


async def domain_ranked_keywords(domain: str, login: str, password: str, limit: int = 10) -> Optional[DomainKeywordsResult]:
    """
    Get keywords a domain currently ranks for.
    Cost: ~$0.01 per call.
    """
    domain = domain.replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")
    result = DomainKeywordsResult(domain=domain)

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(
                f"{BASE_URL}/dataforseo_labs/google/ranked_keywords/live",
                headers=_auth_header(login, password),
                json=[{"target": domain, "limit": limit, "order_by": ["keyword_data.keyword_info.search_volume,desc"]}],
            )
            if r.status_code != 200:
                return None

            data = r.json()
            tasks = data.get("tasks", [])
            if not tasks or not tasks[0].get("result"):
                return None

            res = tasks[0]["result"][0]
            result.total_keywords = res.get("total_count", 0)

            metrics = res.get("metrics", {}).get("organic", {})
            result.organic_traffic = metrics.get("etv", 0) or 0

            for item in res.get("items", [])[:limit]:
                kw_data = item.get("keyword_data", {})
                kw_info = kw_data.get("keyword_info", {})
                ranked = item.get("ranked_serp_element", {}) or {}
                result.top_keywords.append(RankedKeyword(
                    keyword=kw_data.get("keyword", ""),
                    position=ranked.get("serp_item", {}).get("rank_absolute", 0) if isinstance(ranked.get("serp_item"), dict) else 0,
                    search_volume=kw_info.get("search_volume", 0),
                    url=ranked.get("serp_item", {}).get("url", "") if isinstance(ranked.get("serp_item"), dict) else "",
                ))

        except Exception:
            return None

    return result


# ============================================================
# Backlinks Summary — domain authority proxy
# ============================================================

@dataclass
class BacklinksResult:
    domain: str = ""
    rank: int = 0  # DataForSEO domain rank (like DA)
    backlinks_total: int = 0
    referring_domains: int = 0
    referring_domains_nofollow: int = 0


async def backlinks_summary(domain: str, login: str, password: str) -> Optional[BacklinksResult]:
    """
    Get backlink profile summary for a domain.
    Cost: ~$0.01 per call.
    """
    domain = domain.replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")
    result = BacklinksResult(domain=domain)

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(
                f"{BASE_URL}/backlinks/summary/live",
                headers=_auth_header(login, password),
                json=[{"target": domain}],
            )
            if r.status_code != 200:
                return None

            data = r.json()
            tasks = data.get("tasks", [])
            if not tasks or not tasks[0].get("result"):
                return None

            res = tasks[0]["result"][0]
            result.rank = res.get("rank", 0)
            result.backlinks_total = res.get("backlinks", 0)
            result.referring_domains = res.get("referring_domains", 0)
            result.referring_domains_nofollow = res.get("referring_domains_nofollow", 0)

        except Exception:
            return None

    return result
