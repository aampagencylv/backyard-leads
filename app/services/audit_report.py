"""
AI Findability Audit Report Generator

Runs a comprehensive audit on a business website and generates
a branded HTML report that can be hosted at audit.backyardmarketingpros.com.

The audit focuses on AI discoverability (not traditional SEO basics)
because that's the differentiator — every agency talks about SEO,
nobody talks about whether ChatGPT can find your business.
"""
from __future__ import annotations
import json
import re
from typing import Optional, List, Dict
from datetime import datetime, timezone
from dataclasses import dataclass, field

from app.services.website_intel import analyze_website, WebsiteAnalysis
from app.services.local_seo_intel import analyze_local_seo, LocalSEOAnalysis
from app.services.dataforseo import (
    onpage_instant, serp_check, domain_ranked_keywords, backlinks_summary,
    OnPageResult, SERPResult, DomainKeywordsResult, BacklinksResult,
)


@dataclass
class AuditReport:
    """Complete audit data that feeds the HTML report template."""
    company_name: str
    website: str
    city: str = ""
    state: str = ""
    business_type: str = ""
    generated_at: str = ""

    # Scores (0-100)
    ai_findability_score: int = 0
    content_citability_score: int = 0
    local_seo_score: int = 0
    overall_grade: str = ""  # A, B, C, D, F

    # AI Findability details
    has_llms_txt: bool = False
    robots_blocks_ai: bool = False
    ai_crawler_status: Dict[str, str] = field(default_factory=dict)
    has_faq_schema: bool = False
    has_howto_schema: bool = False
    has_speakable_schema: bool = False
    has_about_page: bool = False
    has_team_page: bool = False
    has_credentials: bool = False

    # Content analysis
    word_count: int = 0
    header_count: int = 0
    list_count: int = 0
    stat_count: int = 0
    has_answer_patterns: bool = False

    # Local SEO
    has_local_business_schema: bool = False
    schema_type: str = ""
    nap_found: bool = False
    nap_in_footer: bool = False
    title_has_city: bool = False
    title_has_service: bool = False
    has_map_embed: bool = False
    has_click_to_call: bool = False
    service_page_count: int = 0
    citation_signals: List[str] = field(default_factory=list)

    # Website basics
    load_time: float = 0
    has_ssl: bool = True
    has_blog: bool = False
    has_social_links: bool = False
    tech_stack: List[str] = field(default_factory=list)
    page_title: str = ""

    # DataForSEO data
    domain_rank: int = 0  # Like DA (0-1000 scale)
    backlinks_total: int = 0
    referring_domains: int = 0
    total_ranked_keywords: int = 0
    organic_traffic_estimate: int = 0
    top_keywords: List[Dict] = field(default_factory=list)  # [{keyword, position, volume}]
    serp_competitors: List[Dict] = field(default_factory=list)  # Top 5 from SERP
    has_ai_overview: bool = False  # Google shows AI overview for their keyword
    has_local_pack: bool = False
    has_featured_snippet: bool = False
    schema_types_detected: List[str] = field(default_factory=list)  # From on-page API
    images_without_alt: int = 0
    internal_links: int = 0
    external_links: int = 0
    has_sitemap: bool = False

    # Google presence
    rating: float = 0
    review_count: int = 0

    # All findings (sorted by priority)
    findings: List[Dict] = field(default_factory=list)
    top_findings: List[Dict] = field(default_factory=list)  # Top 5 for email/card


async def generate_audit(
    website: str,
    company_name: str,
    city: str = "",
    state: str = "",
    business_type: str = "",
    rating: float = 0,
    review_count: int = 0,
) -> AuditReport:
    """
    Run a comprehensive AI findability audit.
    Combines website_intel + local_seo_intel + additional analysis.
    """
    report = AuditReport(
        company_name=company_name,
        website=website,
        city=city,
        state=state,
        business_type=business_type,
        rating=rating,
        review_count=review_count,
        generated_at=datetime.now(timezone.utc).strftime("%B %d, %Y"),
    )

    # Run both analyses
    web_analysis = await analyze_website(website)
    seo_analysis = await analyze_local_seo(website, company_name, business_type)

    # Pull data from web analysis
    report.load_time = web_analysis.load_time_seconds or 0
    report.has_ssl = web_analysis.has_ssl
    report.has_blog = web_analysis.has_blog
    report.has_social_links = web_analysis.has_social_links
    report.tech_stack = web_analysis.tech_stack
    report.page_title = web_analysis.page_title
    report.word_count = len(web_analysis.raw_text_sample.split()) if web_analysis.raw_text_sample else 0

    # Pull data from SEO analysis
    report.ai_findability_score = seo_analysis.ai_visibility_score
    report.content_citability_score = seo_analysis.content_citability_score
    report.local_seo_score = seo_analysis.score
    report.has_llms_txt = seo_analysis.has_llms_txt
    report.robots_blocks_ai = seo_analysis.robots_blocks_ai
    report.ai_crawler_status = seo_analysis.ai_crawler_status
    report.has_faq_schema = seo_analysis.has_faq_schema
    report.has_howto_schema = seo_analysis.has_howto_schema
    report.has_speakable_schema = seo_analysis.has_speakable_schema
    report.has_about_page = seo_analysis.has_about_page
    report.has_team_page = seo_analysis.has_team_page
    report.has_local_business_schema = seo_analysis.has_local_business_schema
    report.schema_type = seo_analysis.schema_type
    report.nap_found = seo_analysis.nap_found
    report.nap_in_footer = seo_analysis.nap_in_footer
    report.title_has_city = seo_analysis.title_has_city
    report.title_has_service = seo_analysis.title_has_service
    report.has_map_embed = seo_analysis.has_map_embed
    report.has_click_to_call = seo_analysis.has_click_to_call
    report.service_page_count = seo_analysis.service_page_count
    report.citation_signals = seo_analysis.citation_signals

    # DataForSEO enrichment (if credentials available)
    from app.config import settings
    dfs_login = settings.dataforseo_login
    dfs_pass = settings.dataforseo_password

    if dfs_login and dfs_pass:
        # Backlinks + domain authority
        try:
            bl = await backlinks_summary(website, dfs_login, dfs_pass)
            if bl:
                report.domain_rank = bl.rank
                report.backlinks_total = bl.backlinks_total
                report.referring_domains = bl.referring_domains
        except Exception:
            pass

        # Ranked keywords
        try:
            kw = await domain_ranked_keywords(website, dfs_login, dfs_pass, limit=10)
            if kw:
                report.total_ranked_keywords = kw.total_keywords
                report.organic_traffic_estimate = kw.organic_traffic
                report.top_keywords = [
                    {"keyword": k.keyword, "position": k.position, "volume": k.search_volume}
                    for k in kw.top_keywords
                ]
        except Exception:
            pass

        # SERP check — who ranks for their main keyword
        if business_type and city:
            try:
                search_term = f"{business_type} {city} {state}".strip()
                location_name = f"{city},{state},United States" if state else f"{city},United States"
                serp = await serp_check(search_term, location_name, dfs_login, dfs_pass)
                if serp:
                    report.has_ai_overview = serp.has_ai_overview
                    report.has_local_pack = serp.has_local_pack
                    report.has_featured_snippet = serp.has_featured_snippet
                    report.serp_competitors = [
                        {"rank": c.rank, "domain": c.domain, "title": c.title,
                         "url": c.url, "is_featured": c.is_featured_snippet}
                        for c in serp.competitors[:5]
                    ]
            except Exception:
                pass

        # On-page technical audit
        try:
            onpage = await onpage_instant(website, dfs_login, dfs_pass)
            if onpage:
                report.schema_types_detected = onpage.schema_types
                report.images_without_alt = onpage.images_without_alt
                report.internal_links = onpage.internal_links
                report.external_links = onpage.external_links
                report.has_sitemap = onpage.has_sitemap
                if onpage.word_count:
                    report.word_count = onpage.word_count
        except Exception:
            pass

    # Merge all findings from both analyses
    all_findings = []
    for p in web_analysis.problems:
        all_findings.append(p)
    for f in seo_analysis.findings:
        all_findings.append({
            "type": f.get("issue", ""),
            "severity": f.get("category", "medium"),
            "detail": f.get("detail", ""),
            "angle": f.get("talking_point", ""),
        })

    # Add DataForSEO-derived findings
    if report.total_ranked_keywords == 0:
        all_findings.append({
            "type": "No ranked keywords",
            "severity": "high",
            "detail": "Your website doesn't rank for any keywords on Google",
            "angle": "Your website isn't appearing in Google search results for any keywords. This means every potential customer searching for your services is finding your competitors instead.",
        })
    elif report.total_ranked_keywords < 10:
        all_findings.append({
            "type": "Very few ranked keywords",
            "severity": "medium",
            "detail": f"Only ranking for {report.total_ranked_keywords} keywords",
            "angle": f"Your site only ranks for {report.total_ranked_keywords} keywords. Competitors in your market typically rank for 50-200+. Each keyword you're missing is a customer going somewhere else.",
        })

    if report.domain_rank > 0 and report.domain_rank < 20:
        all_findings.append({
            "type": "Low domain authority",
            "severity": "medium",
            "detail": f"Domain authority score: {report.domain_rank}",
            "angle": f"Your website's authority score is {report.domain_rank}. This affects how Google and AI rank your content. Building quality backlinks from local directories and industry sites would improve this.",
        })

    if report.referring_domains < 10:
        all_findings.append({
            "type": "Few referring domains",
            "severity": "medium",
            "detail": f"Only {report.referring_domains} websites link to yours",
            "angle": f"Only {report.referring_domains} other websites link to yours. AI search engines use backlinks as a trust signal. Chamber of Commerce, BBB, Houzz, and local news sites are easy wins.",
        })

    # Sort by severity
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_findings.sort(key=lambda f: severity_order.get(f.get("severity", "low"), 3))
    report.findings = all_findings
    report.top_findings = all_findings[:5]

    # Calculate overall grade
    avg = (report.ai_findability_score + report.content_citability_score + report.local_seo_score) / 3
    if avg >= 80:
        report.overall_grade = "A"
    elif avg >= 60:
        report.overall_grade = "B"
    elif avg >= 40:
        report.overall_grade = "C"
    elif avg >= 20:
        report.overall_grade = "D"
    else:
        report.overall_grade = "F"

    return report


DEFAULT_HEADER_BANNER = "/static/report-banner.jpg"
DEFAULT_FOOTER_LOGO = "https://backyardmarketingpros.com/wp-content/uploads/2024/08/BMP_Logo_Color_Horiz-1024x269.png"


async def ensure_audit_for_company(db, company) -> Optional[str]:
    """Get-or-create an audit report URL for a company. Used by the
    sequence generators so emails / iMessage steps can naturally
    reference the audit they ran.

    Behavior:
      - Existing AuditReportModel? Return its public URL.
      - No audit but company has a website + problems_found? Generate
        one synchronously and persist + return URL.
      - Anything else (no website, generation fails, etc.) → None.
        Callers should treat None as 'skip the audit reference' so
        sequence creation never breaks because of an audit issue.
    """
    from sqlalchemy import select as _s
    from app.models import AuditReportModel
    from app.config import settings as _settings
    import secrets as _secrets
    import json as _json
    import logging
    log = logging.getLogger("bmp.audit_report")

    if not company or not getattr(company, "id", None):
        return None
    public_url = _settings.public_url.rstrip("/")

    existing = (await db.execute(
        _s(AuditReportModel).where(AuditReportModel.company_id == company.id)
    )).scalar_one_or_none()
    if existing:
        return f"{public_url}/report/{existing.token}"

    if not company.website:
        return None

    try:
        audit = await generate_audit(
            website=company.website,
            company_name=company.name,
            city=company.city or "",
            state=company.state or "",
            business_type=company.business_type or "",
            rating=company.rating or 0,
            review_count=company.review_count or 0,
        )
        token = _secrets.token_urlsafe(16)
        # Resolve org branding (avoids a circular import — done inline)
        from app.runtime_config import _get_or_create as _get_rc
        from app.routes.audit_routes import _resolve_audit_booking_url, _resolve_audit_assets
        rc = await _get_rc(db)
        booking_url = await _resolve_audit_booking_url(db, rc, public_url)
        assets = await _resolve_audit_assets(db, rc)
        html = render_report_html(
            audit, token, public_url,
            booking_url_override=booking_url,
            **assets,
        )
        row = AuditReportModel(
            company_id=company.id,
            token=token,
            html_content=html,
            ai_findability_score=audit.ai_findability_score,
            content_citability_score=audit.content_citability_score,
            local_seo_score=audit.local_seo_score,
            overall_grade=audit.overall_grade,
            findings_json=_json.dumps([{
                "type": f.get("type", ""), "severity": f.get("severity", "medium"),
                "detail": f.get("detail", ""), "angle": f.get("angle", ""),
            } for f in audit.top_findings]),
        )
        db.add(row)
        await db.flush()  # commit deferred to caller
        return f"{public_url}/report/{token}"
    except Exception as e:
        log.warning(f"ensure_audit_for_company({company.id}) failed: {e}")
        return None


def render_report_html(
    report: AuditReport, token: str, public_url: str = "",
    *, header_url: str = "", footer_logo_url: str = "",
    left_image_url: str = "", left_message: str = "",
    right_image_url: str = "", right_message: str = "",
    booking_url_override: str = "",
) -> str:
    """Render the audit report as a branded HTML page.

    Kwargs are all optional org-level overrides set in Settings →
    Audit Reports. Empty strings fall back to defaults.

    Side panels: when EITHER side has an image or message, we switch
    from the single-column centered layout to a 3-column grid with
    sticky sidebars (collapses to stacked on narrow viewports).

    booking_url_override: when set, replaces the default iClosed URL
    on the 'Schedule a Discovery Call' CTAs. Used to route to the
    native scheduler (/book/{slug}) or a custom URL."""
    header_img = (header_url or "").strip() or DEFAULT_HEADER_BANNER
    footer_img = (footer_logo_url or "").strip() or DEFAULT_FOOTER_LOGO
    left_img = (left_image_url or "").strip()
    left_msg = (left_message or "").strip()
    right_img = (right_image_url or "").strip()
    right_msg = (right_message or "").strip()
    has_left = bool(left_img or left_msg)
    has_right = bool(right_img or right_msg)
    has_sides = has_left or has_right

    def score_color(score):
        if score >= 70:
            return "#1B5E20"
        elif score >= 40:
            return "#E65100"
        else:
            return "#c0392b"

    def score_emoji(score):
        if score >= 70:
            return "&#x1F7E2;"  # green circle
        elif score >= 40:
            return "&#x1F7E1;"  # yellow circle
        else:
            return "&#x1F534;"  # red circle

    def check_icon(val):
        return "&#x2705;" if val else "&#x274C;"

    findings_html = ""
    for i, f in enumerate(report.findings[:10]):
        sev = f.get("severity", "medium")
        sev_color = {"critical": "#c0392b", "high": "#E65100", "medium": "#f39c12", "low": "#888"}.get(sev, "#888")
        angle = f.get("angle", f.get("detail", ""))
        findings_html += f"""
        <div style="padding:16px;margin-bottom:12px;background:#fff;border-radius:8px;border-left:4px solid {sev_color}">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
                <strong style="font-size:14px">{i+1}. {_esc(f.get('type', f.get('detail', '')[:50]))}</strong>
                <span style="background:{sev_color}22;color:{sev_color};padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;text-transform:uppercase">{sev}</span>
            </div>
            <p style="margin:0;font-size:13px;color:#555;line-height:1.5">{_esc(angle)}</p>
        </div>
        """

    ai_checks_html = f"""
    <table style="width:100%;border-collapse:collapse;font-size:13px">
        <tr style="border-bottom:1px solid #eee"><td style="padding:8px">{check_icon(report.has_llms_txt)} llms.txt file</td><td style="padding:8px;color:#666">{'Found' if report.has_llms_txt else 'Missing — AI engines have to guess what your business does'}</td></tr>
        <tr style="border-bottom:1px solid #eee"><td style="padding:8px">{check_icon(not report.robots_blocks_ai)} AI crawlers allowed</td><td style="padding:8px;color:#666">{'Allowed' if not report.robots_blocks_ai else 'BLOCKED — ChatGPT, Claude, and Perplexity cannot access your site'}</td></tr>
        <tr style="border-bottom:1px solid #eee"><td style="padding:8px">{check_icon(report.has_faq_schema)} FAQ schema</td><td style="padding:8px;color:#666">{'Found' if report.has_faq_schema else 'Missing — AI answer boxes pull from FAQ schema first'}</td></tr>
        <tr style="border-bottom:1px solid #eee"><td style="padding:8px">{check_icon(report.has_about_page or report.has_team_page)} About/Team page</td><td style="padding:8px;color:#666">{'Found' if report.has_about_page or report.has_team_page else 'Missing — AI evaluates trust before recommending businesses'}</td></tr>
        <tr style="border-bottom:1px solid #eee"><td style="padding:8px">{check_icon(report.has_local_business_schema)} LocalBusiness schema</td><td style="padding:8px;color:#666">{'Found' + (f' ({report.schema_type})' if report.schema_type else '') if report.has_local_business_schema else 'Missing — Google and AI cannot properly identify your business type'}</td></tr>
        <tr><td style="padding:8px">{check_icon(report.content_citability_score >= 50)} Citable content</td><td style="padding:8px;color:#666">Score: {report.content_citability_score}/100 — {'Good' if report.content_citability_score >= 50 else 'Your content is not structured for AI to quote'}</td></tr>
    </table>
    """

    compare_url = f"{public_url}/report/{token}/compare" if public_url else f"/report/{token}/compare"
    # CTA booking destination. When the audit settings haven't been
    # touched we still default to the iClosed URL (preserves existing
    # behavior). The caller can override to point at the native
    # scheduler /book/{slug} or any custom URL.
    from app.config import settings as _settings
    default_iclosed = _settings.iclosed_booking_url or "https://app.iclosed.io/e/backyardmarketingpros/discovery-call"
    booking_url = (booking_url_override or "").strip() or default_iclosed

    # Side panels — rendered only when content is configured. Each side
    # can be: just an image, just a message, or both. When both sides
    # are empty we keep the original single-column centered layout
    # (no layout change for existing reports).
    def _render_side(img: str, msg: str) -> str:
        if not (img or msg):
            return ""
        parts = ['<aside style="position:sticky;top:20px">']
        if img:
            parts.append(
                f'<img src="{_esc(img)}" alt="" '
                'style="width:100%;border-radius:10px;display:block;margin-bottom:12px;'
                'box-shadow:0 2px 8px rgba(0,0,0,0.06)" '
                'onerror="this.style.display=\'none\'">'
            )
        if msg:
            # Convert newlines to <br>; trim aggressively long content
            safe_msg = _esc(msg[:1500]).replace("\n", "<br>")
            parts.append(
                '<div style="background:white;border-radius:10px;padding:14px 16px;'
                'font-size:13px;line-height:1.6;color:#444;'
                'box-shadow:0 2px 8px rgba(0,0,0,0.06)">'
                f'{safe_msg}</div>'
            )
        parts.append("</aside>")
        return "".join(parts)

    left_panel = _render_side(left_img, left_msg)
    right_panel = _render_side(right_img, right_msg)

    if has_sides:
        # Three-column grid layout. The grid template adapts to which
        # sides are present so a single sidebar gets more space.
        if has_left and has_right:
            grid_cols = "minmax(180px,240px) minmax(0,1fr) minmax(180px,240px)"
        elif has_left:
            grid_cols = "minmax(180px,260px) minmax(0,1fr)"
        else:
            grid_cols = "minmax(0,1fr) minmax(180px,260px)"
        outer_open = (
            f'<div style="max-width:1200px;margin:0 auto;padding:20px">'
            f'<div style="display:grid;grid-template-columns:{grid_cols};'
            'gap:24px;align-items:start">'
            + left_panel
        )
        outer_close = right_panel + '</div></div>'
        # In the grid mode, our inner .container shouldn't apply its
        # own max-width — override via inline style on the wrapping div.
        container_open = '<div style="padding:0;max-width:none">'
    else:
        outer_open = ""
        outer_close = ""
        container_open = '<div class="container">'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI Findability Report — {_esc(report.company_name)}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f7f5; color: #1a1a1a; }}
        .container {{ max-width: 800px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #0D3B13 0%, #1B5E20 100%); color: white; padding: 40px; border-radius: 12px; margin-bottom: 24px; }}
        .header img {{ width: 200px; margin-bottom: 20px; }}
        .header h1 {{ font-size: 28px; margin-bottom: 8px; }}
        .header p {{ color: rgba(255,255,255,0.8); font-size: 14px; }}
        .scores {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 24px; }}
        .score-card {{ background: white; border-radius: 12px; padding: 24px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
        .score-card .number {{ font-size: 48px; font-weight: 700; }}
        .score-card .label {{ font-size: 13px; color: #666; margin-top: 4px; }}
        .section {{ background: white; border-radius: 12px; padding: 24px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
        .section h2 {{ font-size: 18px; color: #1B5E20; margin-bottom: 16px; padding-bottom: 8px; border-bottom: 2px solid #1B5E2022; }}
        .section h3 {{ font-size: 15px; margin-bottom: 12px; }}
        .cta-box {{ background: linear-gradient(135deg, #E65100, #EF6C00); color: white; border-radius: 12px; padding: 32px; text-align: center; margin: 24px 0; }}
        .cta-box h2 {{ font-size: 22px; margin-bottom: 8px; }}
        .cta-box p {{ margin-bottom: 16px; color: rgba(255,255,255,0.9); }}
        .cta-box a {{ display: inline-block; background: white; color: #E65100; padding: 12px 32px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 15px; }}
        .cta-box a:hover {{ background: #f5f5f5; }}
        .grade {{ display: inline-block; width: 60px; height: 60px; border-radius: 50%; font-size: 28px; font-weight: 700; line-height: 60px; text-align: center; color: white; }}
        .footer {{ text-align: center; padding: 24px; color: #888; font-size: 12px; }}
        @media print {{ body {{ background: white; }} .container {{ padding: 0; }} }}
        @media (max-width: 600px) {{ .scores {{ grid-template-columns: 1fr; }} .header {{ padding: 24px; }} }}
        /* Side-panel grid → stack on narrow viewports */
        @media (max-width: 900px) {{
            body > div[style*="display:grid"] {{
                grid-template-columns: 1fr !important;
            }}
            aside[style*="position:sticky"] {{
                position: static !important;
            }}
        }}
    </style>
</head>
<body>
    {outer_open}
    {container_open}
        <div style="border-radius:12px;overflow:hidden;margin-bottom:24px;box-shadow:0 4px 16px rgba(0,0,0,0.1)">
            <img src="{_esc(header_img)}" alt="Header" style="width:100%;display:block;background:#0D3B13" onerror="this.style.display='none'">
            <div style="background:linear-gradient(135deg, #0D3B13 0%, #1B5E20 100%);color:white;padding:32px 40px;display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap">
                <div>
                    <h1 style="font-size:28px;margin-bottom:8px">AI Findability Report</h1>
                    <p style="color:rgba(255,255,255,0.8);font-size:16px;margin-bottom:4px">{_esc(report.company_name)} &middot; {_esc(report.city)}{', ' + _esc(report.state) if report.state else ''}</p>
                    <p style="color:rgba(255,255,255,0.5);font-size:12px">Generated {report.generated_at}</p>
                </div>
                <a href="{_esc(booking_url)}" style="display:inline-block;background:#FF723F;color:white;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,0.2)">📅 Schedule a Discovery Call</a>
            </div>
        </div>

        <!-- Scores -->
        <div class="scores">
            <div class="score-card">
                <div class="number" style="color:{score_color(report.ai_findability_score)}">{report.ai_findability_score}</div>
                <div class="label">AI Findability</div>
                <div style="font-size:11px;color:#999">out of 100</div>
            </div>
            <div class="score-card">
                <div class="number" style="color:{score_color(report.content_citability_score)}">{report.content_citability_score}</div>
                <div class="label">Content Citability</div>
                <div style="font-size:11px;color:#999">out of 100</div>
            </div>
            <div class="score-card">
                <div class="number" style="color:{score_color(report.local_seo_score)}">{report.local_seo_score}</div>
                <div class="label">Local SEO</div>
                <div style="font-size:11px;color:#999">out of 100</div>
            </div>
        </div>

        <!-- Executive Summary -->
        <div class="section">
            <h2>Executive Summary</h2>
            <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px">
                <div class="grade" style="background:{score_color(report.ai_findability_score)}">{report.overall_grade}</div>
                <div>
                    <strong style="font-size:16px">Overall Grade: {report.overall_grade}</strong>
                    <p style="font-size:13px;color:#666;margin-top:4px">Based on AI findability, content quality, and local search presence</p>
                </div>
            </div>
            <p style="font-size:14px;line-height:1.6;color:#444">
                When homeowners ask ChatGPT, Google AI Overview, or Perplexity for a
                <strong>{_esc(report.business_type or 'service provider')}</strong> in
                <strong>{_esc(report.city or 'your area')}</strong>, your business
                {'is not showing up' if report.ai_findability_score < 40 else 'has limited visibility' if report.ai_findability_score < 70 else 'has good visibility'}.
                {'This is a significant missed opportunity — ' if report.ai_findability_score < 40 else ''}
                45% of consumers now use AI for local recommendations, and that number is growing fast.
            </p>
            {f'<p style="font-size:14px;line-height:1.6;color:#444;margin-top:8px"><strong>Google Rating:</strong> ★ {report.rating} ({report.review_count} reviews)</p>' if report.rating else ''}
        </div>

        <!-- AI Findability -->
        <div class="section">
            <h2>AI Findability Analysis</h2>
            <p style="font-size:13px;color:#666;margin-bottom:16px">
                AI search engines (ChatGPT, Google AI Overviews, Perplexity, Claude) decide whether to
                recommend your business based on these signals. Unlike traditional SEO, this is about
                whether AI can <em>understand, trust, and cite</em> your content.
            </p>
            {ai_checks_html}
        </div>

        <!-- The Shift -->
        <div class="section">
            <h2>Why This Matters Now</h2>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:12px">
                <div style="background:#f8f9fa;padding:16px;border-radius:8px;text-align:center">
                    <div style="font-size:32px;font-weight:700;color:#1B5E20">45%</div>
                    <div style="font-size:12px;color:#666">of consumers now use AI for local recommendations</div>
                </div>
                <div style="background:#f8f9fa;padding:16px;border-radius:8px;text-align:center">
                    <div style="font-size:32px;font-weight:700;color:#E65100">15.9%</div>
                    <div style="font-size:12px;color:#666">ChatGPT conversion rate vs 1.76% for Google organic</div>
                </div>
            </div>
            <p style="font-size:13px;color:#555;line-height:1.6">
                Traditional SEO focuses on ranking in Google's blue links. But AI search engines work differently —
                they don't show a list of websites. They recommend specific businesses by name. If your website
                doesn't give AI the signals it needs (structured data, citable content, trust indicators), you're
                invisible to nearly half of potential customers.
            </p>
        </div>

        <!-- Top Findings -->
        <div class="section">
            <h2>Key Findings ({len(report.findings)})</h2>
            {findings_html}
        </div>

        {'<!-- Search Visibility -->' + chr(10) + '<div class="section"><h2>Search Visibility</h2>' +
        f'<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:16px">' +
        f'<div style="background:#f8f9fa;padding:16px;border-radius:8px;text-align:center"><div style="font-size:28px;font-weight:700;color:#1B5E20">{report.total_ranked_keywords}</div><div style="font-size:12px;color:#666">Keywords ranking on Google</div></div>' +
        f'<div style="background:#f8f9fa;padding:16px;border-radius:8px;text-align:center"><div style="font-size:28px;font-weight:700;color:#E65100">{report.organic_traffic_estimate}</div><div style="font-size:12px;color:#666">Est. monthly organic visits</div></div>' +
        f'<div style="background:#f8f9fa;padding:16px;border-radius:8px;text-align:center"><div style="font-size:28px;font-weight:700">{report.referring_domains}</div><div style="font-size:12px;color:#666">Referring domains</div></div>' +
        '</div>' +
        (('<h3 style="font-size:14px;margin-bottom:8px">Your Top Keywords</h3><table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr style="border-bottom:1px solid #ddd"><th style="padding:6px;text-align:left">Keyword</th><th style="padding:6px;text-align:center">Position</th><th style="padding:6px;text-align:right">Monthly Searches</th></tr></thead><tbody>' +
        ''.join(f'<tr style="border-bottom:1px solid #eee"><td style="padding:6px">{_esc(k.get("keyword",""))}</td><td style="padding:6px;text-align:center">{k.get("position","-")}</td><td style="padding:6px;text-align:right">{k.get("volume",0):,}</td></tr>' for k in report.top_keywords[:7]) +
        '</tbody></table>') if report.top_keywords else '<p style="font-size:13px;color:#888">No keyword rankings detected — your website is not appearing in Google search results.</p>') +
        (f'<div style="margin-top:12px;padding:12px;background:#FFF8F0;border-radius:8px;font-size:13px"><strong>Google SERP Features for your market:</strong> ' +
        (' AI Overview appears' if report.has_ai_overview else '') +
        (' | Local Pack appears' if report.has_local_pack else '') +
        (' | Featured Snippet appears' if report.has_featured_snippet else '') +
        ('' if (report.has_ai_overview or report.has_local_pack or report.has_featured_snippet) else 'Standard organic results') +
        '</div>' if report.serp_competitors else '') +
        '</div>' if (report.total_ranked_keywords or report.top_keywords or report.serp_competitors) else ''}

        {('<!-- Who Ranks Above You -->' + chr(10) + '<div class="section"><h2>Who Ranks Above You</h2>' +
        f'<p style="font-size:13px;color:#666;margin-bottom:12px">Top results for <strong>"{_esc(report.business_type)} {_esc(report.city)}"</strong> on Google:</p>' +
        '<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr style="border-bottom:1px solid #ddd"><th style="padding:6px;text-align:left">#</th><th style="padding:6px;text-align:left">Website</th><th style="padding:6px;text-align:left">Title</th></tr></thead><tbody>' +
        ''.join(f'<tr style="border-bottom:1px solid #eee"><td style="padding:6px;font-weight:600">{c.get("rank","")}</td><td style="padding:6px;color:#0077B5">{_esc(c.get("domain",""))}</td><td style="padding:6px;color:#555">{_esc(c.get("title","")[:60])}</td></tr>' for c in report.serp_competitors[:5]) +
        '</tbody></table></div>') if report.serp_competitors else ''}

        <!-- Competitor CTA -->
        <div class="cta-box">
            <h2>How do you compare to your competitors?</h2>
            <p>We'll run this same audit on the top 3 businesses in your market and show you exactly where you stand.</p>
            <a href="{_esc(compare_url)}" id="compare-btn">See Your Competitive Comparison</a>
        </div>

        <!-- Local SEO -->
        <div class="section">
            <h2>Local Search Presence</h2>
            <table style="width:100%;border-collapse:collapse;font-size:13px">
                <tr style="border-bottom:1px solid #eee"><td style="padding:8px">{check_icon(report.title_has_city)} City in page title</td><td style="padding:8px;color:#666">{'Yes' if report.title_has_city else 'Missing — critical for local search'}</td></tr>
                <tr style="border-bottom:1px solid #eee"><td style="padding:8px">{check_icon(report.title_has_service)} Service in page title</td><td style="padding:8px;color:#666">{'Yes' if report.title_has_service else 'Missing'}</td></tr>
                <tr style="border-bottom:1px solid #eee"><td style="padding:8px">{check_icon(report.nap_found)} Phone visible on site</td><td style="padding:8px;color:#666">{'Yes' if report.nap_found else 'No phone number found'}</td></tr>
                <tr style="border-bottom:1px solid #eee"><td style="padding:8px">{check_icon(report.has_click_to_call)} Click-to-call</td><td style="padding:8px;color:#666">{'Yes' if report.has_click_to_call else 'No tel: link — mobile visitors cannot tap to call'}</td></tr>
                <tr style="border-bottom:1px solid #eee"><td style="padding:8px">{check_icon(report.has_map_embed)} Google Maps embed</td><td style="padding:8px;color:#666">{'Yes' if report.has_map_embed else 'Missing'}</td></tr>
                <tr><td style="padding:8px">{check_icon(report.service_page_count >= 3)} Dedicated service pages</td><td style="padding:8px;color:#666">{report.service_page_count} found {'(good)' if report.service_page_count >= 3 else '— more pages = more ranking opportunities'}</td></tr>
            </table>
            {f'<div style="margin-top:12px;font-size:12px;color:#888">Citations found: {", ".join(report.citation_signals) if report.citation_signals else "None detected"}</div>' if True else ''}
        </div>

        <!-- Website Performance -->
        <div class="section">
            <h2>Website Performance</h2>
            <table style="width:100%;border-collapse:collapse;font-size:13px">
                <tr style="border-bottom:1px solid #eee"><td style="padding:8px">{check_icon(report.load_time < 4)} Load time</td><td style="padding:8px;color:#666">{report.load_time:.1f}s {'(good)' if report.load_time < 3 else '(slow — over 3s loses visitors)' if report.load_time < 6 else '(very slow)'}</td></tr>
                <tr style="border-bottom:1px solid #eee"><td style="padding:8px">{check_icon(report.has_ssl)} HTTPS</td><td style="padding:8px;color:#666">{'Secure' if report.has_ssl else 'Not secure — browsers show a warning'}</td></tr>
                <tr style="border-bottom:1px solid #eee"><td style="padding:8px">{check_icon(report.has_blog)} Blog/content</td><td style="padding:8px;color:#666">{'Found' if report.has_blog else 'No blog — limits content for AI to cite'}</td></tr>
                <tr><td style="padding:8px">Tech stack</td><td style="padding:8px;color:#666">{', '.join(report.tech_stack) if report.tech_stack else 'Not detected'}</td></tr>
            </table>
        </div>

        <!-- Final CTA — direct iClosed booking -->
        <div class="section" style="text-align:center;padding:32px">
            <h2 style="border:none;text-align:center">Ready to get found by AI?</h2>
            <p style="font-size:14px;color:#666;margin:12px 0 20px">
                We help {_esc(report.business_type or 'backyard')} professionals get discovered by
                ChatGPT, Google AI, and Perplexity. Pick a time below — we'll walk through what we found and the fastest fixes.
            </p>
            <a href="{_esc(booking_url)}" style="display:inline-block;background:#E65100;color:white;padding:14px 32px;border-radius:8px;text-decoration:none;font-weight:600;font-size:15px">📅 Schedule a Discovery Call</a>
        </div>

        <div class="footer">
            <img src="{_esc(footer_img)}" style="max-width:200px;max-height:60px;margin-bottom:8px" alt="Logo" onerror="this.style.display='none'">
            <p>Backyard Marketing Pros &middot; A Division of AAMP Agency</p>
            <p style="margin-top:4px">backyardmarketingpros.com</p>
        </div>
    </div>
    {outer_close}

    <!-- Tracking beacon -->
    <script>
    (function() {{
        fetch('/api/track/report-view', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{ token: '{_esc(token)}' }})
        }}).catch(function() {{}});
    }})();
    </script>
</body>
</html>"""


def _esc(s):
    """HTML escape."""
    if not s:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
