"""
Generate BDR-friendly talking points from audit/enrichment data.
These appear on the company detail right-hand panel so the rep
knows exactly what to say on a call or in an email.

No AI needed — just conditional logic turning data into plain English.
"""
from __future__ import annotations
from typing import Optional, List, Dict
import json


def generate_talking_points(
    company_name: str,
    problems: List[Dict],
    ai_findability_score: int = 0,
    content_citability_score: int = 0,
    local_seo_score: int = 0,
    total_ranked_keywords: int = 0,
    referring_domains: int = 0,
    domain_rank: int = 0,
    review_count: int = 0,
    rating: float = 0,
    has_llms_txt: bool = False,
    has_faq_schema: bool = False,
    employee_count: int = 0,
    serp_competitors: List[Dict] = None,
) -> List[Dict]:
    """
    Returns a list of talking points, each with:
    - topic: short label
    - point: what to say (in human terms)
    - severity: high/medium/low (for visual priority)
    """
    points = []

    # AI Findability — the lead pitch
    if ai_findability_score < 30:
        points.append({
            "topic": "AI Invisible",
            "point": f"When someone asks ChatGPT or Google AI for a recommendation in your area, {company_name} doesn't show up at all. Right now 45% of consumers are using AI to find local services — that number is growing every month.",
            "severity": "high",
        })
    elif ai_findability_score < 60:
        points.append({
            "topic": "Limited AI Visibility",
            "point": f"{company_name} has some visibility in AI search but there are gaps. A few targeted changes could put you ahead of most competitors who haven't addressed this yet.",
            "severity": "medium",
        })

    # llms.txt — concrete and easy to explain
    if not has_llms_txt:
        points.append({
            "topic": "No llms.txt",
            "point": "Your website doesn't have an llms.txt file. Think of it like a business card for AI — it tells ChatGPT and other AI tools exactly what you do, where you serve, and what services you offer. Without it, AI has to guess. Most of your competitors don't have one either, so adding it now puts you ahead.",
            "severity": "high",
        })

    # FAQ Schema
    if not has_faq_schema:
        points.append({
            "topic": "Missing FAQ Schema",
            "point": "When someone asks Google a question like 'how much does a pool cost?' or 'best time to landscape in Phoenix,' Google pulls the answer from websites with FAQ markup. Your site doesn't have this, so Google skips over you and shows a competitor's answer instead.",
            "severity": "high",
        })

    # Keywords
    if total_ranked_keywords == 0:
        points.append({
            "topic": "Not Ranking on Google",
            "point": f"We checked and {company_name} isn't ranking for any keywords on Google right now. That means when potential customers search for your services, they're finding other businesses instead. This is fixable — it usually takes 60-90 days to start showing up.",
            "severity": "high",
        })
    elif total_ranked_keywords < 20:
        points.append({
            "topic": "Few Keywords Ranking",
            "point": f"You're only ranking for {total_ranked_keywords} keywords on Google. Most successful businesses in your space rank for 50-200+. Each keyword you're missing is a potential customer going to a competitor.",
            "severity": "medium",
        })

    # Competitors
    if serp_competitors:
        top = serp_competitors[0] if serp_competitors else None
        if top:
            points.append({
                "topic": "Your Top Competitor",
                "point": f"When people Google your services in your area, {top.get('domain', 'your competitor')} shows up first. They're getting the calls that could be going to you. We can show you exactly what they're doing differently.",
                "severity": "medium",
            })

    # Reviews
    if review_count and review_count < 20:
        points.append({
            "topic": "Review Opportunity",
            "point": f"You have {review_count} Google reviews. Businesses with 50+ reviews get significantly more calls. There are simple strategies to get more reviews from your happy customers without being pushy.",
            "severity": "medium",
        })
    elif review_count and review_count >= 50 and rating and rating >= 4.5:
        points.append({
            "topic": "Strong Reviews (Leverage This)",
            "point": f"You have {review_count} reviews at {rating} stars — that's great! But this asset isn't being leveraged in AI search. When we structure your reviews properly, AI tools will cite your reputation when recommending businesses.",
            "severity": "low",
        })

    # Backlinks / Authority
    if referring_domains < 10:
        points.append({
            "topic": "Low Authority",
            "point": f"Only {referring_domains} other websites link to yours. Google and AI use this as a trust signal. Getting listed on your local Chamber of Commerce, BBB, Houzz, and a few industry directories would make a big difference quickly.",
            "severity": "medium",
        })

    # Content citability
    if content_citability_score < 30:
        points.append({
            "topic": "Content Not AI-Friendly",
            "point": "Your website content isn't structured in a way that AI can easily quote or reference. Adding specific numbers, FAQ sections, and clear service descriptions gives AI something concrete to cite when recommending businesses.",
            "severity": "medium",
        })

    return points[:6]  # Max 6 talking points
