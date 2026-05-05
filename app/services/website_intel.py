"""
Website Intelligence Service
Crawls a prospect's website and identifies marketing problems
specific to home service businesses (pool builders, landscapers, etc).
"""
from __future__ import annotations
import httpx
import json
import time
from typing import Optional, List
from bs4 import BeautifulSoup
from dataclasses import dataclass, field


@dataclass
class WebsiteAnalysis:
    url: str
    load_time_seconds: Optional[float] = None
    has_ssl: bool = False
    mobile_friendly: Optional[bool] = None
    has_blog: bool = False
    has_social_links: bool = False
    social_platforms: list[str] = field(default_factory=list)
    has_reviews_page: bool = False
    has_contact_form: bool = False
    has_online_booking: bool = False
    has_gallery: bool = False
    page_title: str = ""
    meta_description: str = ""
    h1_tags: list[str] = field(default_factory=list)
    services_mentioned: list[str] = field(default_factory=list)
    tech_stack: list[str] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)
    raw_text_sample: str = ""  # First ~2000 chars for AI analysis


async def analyze_website(url: str) -> WebsiteAnalysis:
    """
    Crawl a business website and identify marketing problems.
    Returns structured analysis with specific issues found.
    """
    analysis = WebsiteAnalysis(url=url)

    # Normalize URL
    if not url.startswith("http"):
        url = f"https://{url}"

    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; BackyardLeads/1.0)"},
        ) as client:
            # Measure load time
            start = time.time()
            response = await client.get(url)
            analysis.load_time_seconds = round(time.time() - start, 2)

            # Check SSL based on final URL after redirects (not the input URL)
            final_url = str(response.url)
            analysis.has_ssl = final_url.startswith("https")

            if response.status_code != 200:
                analysis.problems.append({
                    "type": "website_down",
                    "severity": "critical",
                    "detail": f"Website returned status {response.status_code}",
                    "angle": "Your website appears to be down or having issues — potential customers can't reach you."
                })
                return analysis

            html = response.text
            soup = BeautifulSoup(html, "lxml")

            # Basic SEO
            _check_seo(soup, analysis)

            # Performance
            _check_performance(analysis)

            # Content & features
            _check_content(soup, html, analysis)

            # Social presence
            _check_social(soup, html, analysis)

            # Home service specific checks
            _check_home_service_features(soup, html, analysis)

            # Extract text sample for AI analysis
            body = soup.find("body")
            if body:
                text = body.get_text(separator=" ", strip=True)
                analysis.raw_text_sample = text[:2000]

            # Detect tech stack
            _detect_tech_stack(html, response.headers, analysis)

    except httpx.TimeoutException:
        analysis.problems.append({
            "type": "timeout",
            "severity": "critical",
            "detail": "Website took over 15 seconds to load",
            "angle": "Your website is extremely slow — most visitors leave after 3 seconds."
        })
    except Exception as e:
        analysis.problems.append({
            "type": "unreachable",
            "severity": "critical",
            "detail": f"Could not reach website: {str(e)[:100]}",
            "angle": "We couldn't access your website — if we can't reach it, neither can your customers."
        })

    return analysis


def _check_seo(soup: BeautifulSoup, analysis: WebsiteAnalysis):
    """Check basic SEO elements."""
    title_tag = soup.find("title")
    analysis.page_title = title_tag.get_text(strip=True) if title_tag else ""

    meta_desc = soup.find("meta", attrs={"name": "description"})
    analysis.meta_description = meta_desc.get("content", "") if meta_desc else ""

    h1s = soup.find_all("h1")
    analysis.h1_tags = [h.get_text(strip=True) for h in h1s[:5]]

    if not analysis.page_title:
        analysis.problems.append({
            "type": "missing_title",
            "severity": "high",
            "detail": "No page title tag found",
            "angle": "Your site has no title tag — Google doesn't know what to show when people search for your services."
        })

    if not analysis.meta_description:
        analysis.problems.append({
            "type": "missing_meta_description",
            "severity": "medium",
            "detail": "No meta description found",
            "angle": "Your site is missing a meta description — you're letting Google decide how to describe your business in search results."
        })

    if not h1s:
        analysis.problems.append({
            "type": "missing_h1",
            "severity": "medium",
            "detail": "No H1 heading found on homepage",
            "angle": "Your homepage doesn't have a clear headline — visitors don't immediately know what you do."
        })


def _check_performance(analysis: WebsiteAnalysis):
    """Check performance indicators."""
    if analysis.load_time_seconds and analysis.load_time_seconds > 4.0:
        analysis.problems.append({
            "type": "slow_load",
            "severity": "high",
            "detail": f"Page loaded in {analysis.load_time_seconds}s (should be under 3s)",
            "angle": f"Your website takes {analysis.load_time_seconds} seconds to load — 53% of mobile visitors leave if a page takes over 3 seconds."
        })

    if not analysis.has_ssl:
        analysis.problems.append({
            "type": "no_ssl",
            "severity": "high",
            "detail": "Website not using HTTPS",
            "angle": "Your site isn't secure (no HTTPS) — Google penalizes this in rankings and browsers show a 'Not Secure' warning to visitors."
        })


def _check_content(soup: BeautifulSoup, html: str, analysis: WebsiteAnalysis):
    """Check for blog, content marketing."""
    blog_indicators = ["blog", "news", "articles", "posts", "insights"]
    links = soup.find_all("a", href=True)
    link_texts = [a.get_text(strip=True).lower() for a in links]
    link_hrefs = [a["href"].lower() for a in links]

    for indicator in blog_indicators:
        if any(indicator in t for t in link_texts) or any(indicator in h for h in link_hrefs):
            analysis.has_blog = True
            break

    if not analysis.has_blog:
        analysis.problems.append({
            "type": "no_blog",
            "severity": "medium",
            "detail": "No blog or content section found",
            "angle": "You don't have a blog — your competitors who do are ranking for searches like 'best pool designs 2025' and getting free leads from Google."
        })


def _check_social(soup: BeautifulSoup, html: str, analysis: WebsiteAnalysis):
    """Check social media presence."""
    social_domains = {
        "facebook.com": "Facebook",
        "instagram.com": "Instagram",
        "twitter.com": "Twitter",
        "x.com": "Twitter/X",
        "youtube.com": "YouTube",
        "tiktok.com": "TikTok",
        "linkedin.com": "LinkedIn",
        "nextdoor.com": "Nextdoor",
    }

    links = soup.find_all("a", href=True)
    for link in links:
        href = link["href"].lower()
        for domain, name in social_domains.items():
            if domain in href:
                analysis.has_social_links = True
                if name not in analysis.social_platforms:
                    analysis.social_platforms.append(name)

    if not analysis.has_social_links:
        analysis.problems.append({
            "type": "no_social",
            "severity": "medium",
            "detail": "No social media links found on website",
            "angle": "There are no social media links on your site — homeowners check Instagram and Facebook before hiring. You're invisible where they're looking."
        })
    elif "Instagram" not in analysis.social_platforms:
        analysis.problems.append({
            "type": "no_instagram",
            "severity": "medium",
            "detail": "No Instagram presence linked",
            "angle": "You're not on Instagram — for visual businesses like yours, Instagram is where homeowners go to see your work before they call."
        })


def _check_home_service_features(soup: BeautifulSoup, html: str, analysis: WebsiteAnalysis):
    """Check for features important to home service businesses."""
    html_lower = html.lower()

    # Online booking/scheduling
    booking_indicators = ["book", "schedule", "appointment", "calendly", "acuity"]
    analysis.has_online_booking = any(ind in html_lower for ind in booking_indicators)

    # Gallery/portfolio
    gallery_indicators = ["gallery", "portfolio", "our work", "projects", "before-and-after"]
    analysis.has_gallery = any(ind in html_lower for ind in gallery_indicators)

    # Contact form
    forms = soup.find_all("form")
    analysis.has_contact_form = len(forms) > 0

    # Reviews/testimonials page
    review_indicators = ["testimonial", "review", "what our customers", "client stories"]
    analysis.has_reviews_page = any(ind in html_lower for ind in review_indicators)

    if not analysis.has_online_booking:
        analysis.problems.append({
            "type": "no_booking",
            "severity": "low",
            "detail": "No online booking/scheduling found",
            "angle": "You don't have online booking — homeowners want to schedule estimates on their time, not wait for a callback."
        })

    if not analysis.has_gallery:
        analysis.problems.append({
            "type": "no_gallery",
            "severity": "medium",
            "detail": "No portfolio/gallery section found",
            "angle": "No project gallery on your site — your best marketing is showing off your work, and right now visitors can't see it."
        })

    if not analysis.has_reviews_page:
        analysis.problems.append({
            "type": "no_testimonials",
            "severity": "medium",
            "detail": "No testimonials/reviews section found",
            "angle": "No reviews or testimonials on your website — 93% of consumers say online reviews impact their buying decisions."
        })


def _detect_tech_stack(html: str, headers: dict, analysis: WebsiteAnalysis):
    """Detect what tech the site is built with."""
    html_lower = html.lower()

    if "wp-content" in html_lower or "wordpress" in html_lower:
        analysis.tech_stack.append("WordPress")
    if "wix.com" in html_lower:
        analysis.tech_stack.append("Wix")
    if "squarespace" in html_lower:
        analysis.tech_stack.append("Squarespace")
    if "shopify" in html_lower:
        analysis.tech_stack.append("Shopify")
    if "weebly" in html_lower:
        analysis.tech_stack.append("Weebly")
    if "godaddy" in html_lower:
        analysis.tech_stack.append("GoDaddy Builder")

    server = headers.get("server", "")
    if server:
        analysis.tech_stack.append(f"Server: {server}")


def analysis_to_dict(analysis: WebsiteAnalysis) -> dict:
    """Convert analysis to a dictionary for storage."""
    return {
        "url": analysis.url,
        "load_time_seconds": analysis.load_time_seconds,
        "has_ssl": analysis.has_ssl,
        "has_blog": analysis.has_blog,
        "has_social_links": analysis.has_social_links,
        "social_platforms": analysis.social_platforms,
        "has_reviews_page": analysis.has_reviews_page,
        "has_contact_form": analysis.has_contact_form,
        "has_online_booking": analysis.has_online_booking,
        "has_gallery": analysis.has_gallery,
        "page_title": analysis.page_title,
        "meta_description": analysis.meta_description,
        "tech_stack": analysis.tech_stack,
        "problems": analysis.problems,
        "problem_count": len(analysis.problems),
    }
