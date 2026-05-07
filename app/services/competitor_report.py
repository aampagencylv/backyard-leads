"""
Competitor Comparison Report Generator

Takes the prospect's audit data + top SERP competitors,
runs quick audits on each competitor, and generates a
side-by-side branded HTML comparison report.
"""
from __future__ import annotations
import json
from typing import Optional, List, Dict
from datetime import datetime, timezone

from app.services.website_intel import analyze_website
from app.services.local_seo_intel import analyze_local_seo
from app.services.dataforseo import backlinks_summary, domain_ranked_keywords


async def audit_competitor(website: str, name: str, dfs_login: str = "", dfs_pass: str = "") -> dict:
    """Run a lightweight audit on a competitor website."""
    result = {
        "name": name,
        "website": website,
        "ai_findability_score": 0,
        "content_citability_score": 0,
        "local_seo_score": 0,
        "ranked_keywords": 0,
        "referring_domains": 0,
        "domain_rank": 0,
        "has_llms_txt": False,
        "has_faq_schema": False,
        "has_local_business_schema": False,
    }

    try:
        seo = await analyze_local_seo(website, name, "")
        result["ai_findability_score"] = seo.ai_visibility_score
        result["content_citability_score"] = seo.content_citability_score
        result["local_seo_score"] = seo.score
        result["has_llms_txt"] = seo.has_llms_txt
        result["has_faq_schema"] = seo.has_faq_schema
        result["has_local_business_schema"] = seo.has_local_business_schema
    except Exception:
        pass

    if dfs_login and dfs_pass:
        try:
            bl = await backlinks_summary(website, dfs_login, dfs_pass)
            if bl:
                result["referring_domains"] = bl.referring_domains
                result["domain_rank"] = bl.rank
        except Exception:
            pass

        try:
            kw = await domain_ranked_keywords(website, dfs_login, dfs_pass, limit=1)
            if kw:
                result["ranked_keywords"] = kw.total_keywords
        except Exception:
            pass

    return result


def render_comparison_html(
    prospect: dict,
    competitors: List[dict],
    company_name: str,
    city: str = "",
    state: str = "",
    business_type: str = "",
) -> str:
    """Render a branded side-by-side comparison report."""

    def score_color(s):
        if s >= 70: return "#1B5E20"
        elif s >= 40: return "#E65100"
        return "#c0392b"

    def score_bar(score, max_score=100):
        pct = min(100, int(score / max_score * 100)) if max_score else 0
        color = score_color(score)
        return f'<div style="background:#eee;border-radius:4px;height:8px;width:100%"><div style="background:{color};border-radius:4px;height:8px;width:{pct}%"></div></div>'

    def check(val):
        return "&#x2705;" if val else "&#x274C;"

    all_entries = [prospect] + competitors

    # Build comparison rows
    metrics = [
        ("AI Findability Score", "ai_findability_score", 100),
        ("Content Citability", "content_citability_score", 100),
        ("Local SEO Score", "local_seo_score", 100),
        ("Ranked Keywords", "ranked_keywords", None),
        ("Referring Domains", "referring_domains", None),
        ("Domain Authority", "domain_rank", None),
    ]

    checks = [
        ("llms.txt File", "has_llms_txt"),
        ("FAQ Schema", "has_faq_schema"),
        ("LocalBusiness Schema", "has_local_business_schema"),
    ]

    # Table header
    cols = len(all_entries)
    header_cells = "".join(
        f'<th style="padding:12px;text-align:center;min-width:140px;{'background:var(--bmp-cream);' if i == 0 else ''}">'
        f'<strong>{"You" if i == 0 else _esc(e["name"][:25])}</strong>'
        f'<div style="font-size:11px;color:#888">{_esc(e.get("website","")[:30])}</div></th>'
        for i, e in enumerate(all_entries)
    )

    # Metric rows
    metric_rows = ""
    for label, key, max_val in metrics:
        cells = ""
        values = [e.get(key, 0) or 0 for e in all_entries]
        best = max(values) if values else 0
        for i, e in enumerate(all_entries):
            val = e.get(key, 0) or 0
            is_best = val == best and val > 0
            style = "background:var(--bmp-cream);" if i == 0 else ""
            cells += f'<td style="padding:10px;text-align:center;{style}"><div style="font-size:18px;font-weight:700;color:{score_color(val) if max_val else "#333"}">{val:,}</div>{score_bar(val, max_val or best or 1) if max_val else ""}</td>'
        metric_rows += f'<tr style="border-bottom:1px solid #eee"><td style="padding:10px;font-weight:500;font-size:13px">{label}</td>{cells}</tr>'

    # Check rows
    check_rows = ""
    for label, key in checks:
        cells = ""
        for i, e in enumerate(all_entries):
            style = "background:var(--bmp-cream);" if i == 0 else ""
            cells += f'<td style="padding:10px;text-align:center;font-size:16px;{style}">{check(e.get(key, False))}</td>'
        check_rows += f'<tr style="border-bottom:1px solid #eee"><td style="padding:10px;font-weight:500;font-size:13px">{label}</td>{cells}</tr>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Competitive Comparison — {_esc(company_name)}</title>
    <style>
        :root {{ --bmp-green: #1B5E20; --bmp-orange: #E65100; --bmp-cream: #FFF8F0; }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f7f5; color: #1a1a1a; }}
        .container {{ max-width: 900px; margin: 0 auto; padding: 20px; }}
        .section {{ background: white; border-radius: 12px; padding: 24px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
        .section h2 {{ font-size: 18px; color: var(--bmp-green); margin-bottom: 16px; }}
        table {{ width: 100%; border-collapse: collapse; }}
        .footer {{ text-align: center; padding: 24px; color: #888; font-size: 12px; }}
    </style>
</head>
<body>
    <div class="container">
        <div style="border-radius:12px;overflow:hidden;margin-bottom:24px;box-shadow:0 4px 16px rgba(0,0,0,0.1)">
            <img src="/static/report-banner.jpg" alt="BMP" style="width:100%;display:block">
            <div style="background:linear-gradient(135deg, #0D3B13, #1B5E20);color:white;padding:32px 40px">
                <h1 style="font-size:26px;margin-bottom:8px">Competitive Comparison</h1>
                <p style="color:rgba(255,255,255,0.8)">{_esc(company_name)} vs. Top Competitors in {_esc(city)}{', ' + _esc(state) if state else ''}</p>
                <p style="color:rgba(255,255,255,0.5);font-size:12px;margin-top:4px">Generated {datetime.now(timezone.utc).strftime("%B %d, %Y")}</p>
            </div>
        </div>

        <div class="section">
            <h2>Head-to-Head Comparison</h2>
            <div style="overflow-x:auto">
                <table>
                    <thead><tr style="border-bottom:2px solid #ddd"><th style="padding:12px"></th>{header_cells}</tr></thead>
                    <tbody>
                        {metric_rows}
                        <tr><td colspan="{cols+1}" style="padding:12px;font-weight:600;color:var(--bmp-green);font-size:13px">AI Readiness Signals</td></tr>
                        {check_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <div class="section">
            <h2>What This Means</h2>
            <p style="font-size:14px;line-height:1.6;color:#444">
                {'Your competitors are ahead of you on AI findability. ' if prospect.get('ai_findability_score', 0) < max(c.get('ai_findability_score', 0) for c in competitors) else 'You are competitive on AI findability, but there is still room to improve. '}
                {'None of your competitors have an llms.txt file yet — this is your opportunity to get ahead. ' if not any(c.get('has_llms_txt') for c in competitors) else 'Some competitors already have llms.txt files, giving them an edge in AI recommendations. '}
                The businesses that act first on AI findability will dominate local search as more consumers shift to ChatGPT, Google AI Overviews, and Perplexity for recommendations.
            </p>
        </div>

        <div style="background:linear-gradient(135deg, var(--bmp-orange), #EF6C00);color:white;border-radius:12px;padding:32px;text-align:center;margin:24px 0">
            <h2 style="font-size:22px;margin-bottom:8px">Ready to get ahead of your competitors?</h2>
            <p style="margin-bottom:16px;color:rgba(255,255,255,0.9)">We can fix everything in this report and get you found by AI before your competition.</p>
            <a href="https://backyardmarketingpros.com/contact" style="display:inline-block;background:white;color:var(--bmp-orange);padding:14px 32px;border-radius:8px;text-decoration:none;font-weight:600">Let's Talk</a>
        </div>

        <div class="footer">
            <img src="https://backyardmarketingpros.com/wp-content/uploads/2024/08/BMP_Logo_Color_Horiz-1024x269.png" style="width:160px;margin-bottom:8px" alt="BMP">
            <p>Backyard Marketing Pros &middot; A Division of AAMP Agency</p>
        </div>
    </div>
</body>
</html>"""


def _esc(s):
    if not s: return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
