"""
AI Email Generator Service
Uses Claude to generate personalized outreach emails based on
specific marketing problems found on the prospect's website.
"""
from __future__ import annotations
import json
from typing import Optional
import anthropic
from app.config import settings


SYSTEM_PROMPT = """You are an email copywriter for Backyard Marketing Pros (backyardmarketingpros.com).
We provide marketing services to backyard professionals: pool builders, landscapers,
outdoor kitchen/BBQ builders, deck builders, and related home service businesses.

Your job is to write cold outreach emails that:
1. Reference a SPECIFIC problem we found on their website (not generic)
2. Sound like a human wrote it (conversational, not salesy)
3. Are short (under 150 words for the body)
4. Don't use buzzwords or marketing jargon
5. Include a clear but soft CTA (no "book a call NOW!")
6. Feel like you're pointing something out to help, not to sell

The tone should be: helpful neighbor who happens to know marketing, not used car salesman.

NEVER use these phrases:
- "I hope this email finds you well"
- "I'd love to pick your brain"
- "synergy" / "leverage" / "optimize"
- "Are you the right person to speak with?"
- "I noticed you're a [industry] business" (too generic)

DO reference specific data points: their actual site speed, missing features, specific competitors ranking above them, etc.
"""


async def generate_cold_email(
    business_name: str,
    business_type: str,
    website: str,
    problems: list[dict],
    contact_name: Optional[str] = None,
    location: Optional[str] = None,
) -> dict:
    """
    Generate a personalized cold email based on problems found.
    Returns dict with 'subject' and 'body'.
    """
    # Pick the top 1-2 most impactful problems to reference
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    sorted_problems = sorted(problems, key=lambda p: severity_order.get(p.get("severity", "low"), 3))
    top_problems = sorted_problems[:2]

    problems_context = json.dumps(top_problems, indent=2)

    greeting = f"Hi {contact_name}" if contact_name else "Hi there"

    user_prompt = f"""Write a cold outreach email for this prospect:

Business: {business_name}
Type: {business_type}
Website: {website}
Location: {location or "Unknown"}
Contact: {contact_name or "Unknown"}

Problems we found on their website:
{problems_context}

Write the email using the most compelling problem as the hook.
Start with "{greeting}" and sign off as the Backyard Marketing Pros team.

Return your response as JSON with exactly these keys:
- "subject": the email subject line (under 50 chars, no clickbait)
- "body": the full email body
"""

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    # Parse the response
    text = response.content[0].text

    # Try to extract JSON from the response
    try:
        # Handle case where response might have markdown code blocks
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        result = json.loads(text)
        return {"subject": result["subject"], "body": result["body"]}
    except (json.JSONDecodeError, KeyError):
        # Fallback: use the raw text
        return {"subject": f"Quick question about {business_name}'s website", "body": text}


async def generate_follow_up(
    business_name: str,
    business_type: str,
    problems: list[dict],
    previous_email_subject: str,
    follow_up_number: int = 1,
    contact_name: Optional[str] = None,
) -> dict:
    """Generate a follow-up email."""
    problems_context = json.dumps(problems[:2], indent=2)
    greeting = f"Hi {contact_name}" if contact_name else "Hi"

    user_prompt = f"""Write follow-up #{follow_up_number} for this prospect who didn't respond.

Business: {business_name}
Type: {business_type}
Previous email subject: {previous_email_subject}
Contact: {contact_name or "Unknown"}

Problems from their site:
{problems_context}

Rules for follow-up #{follow_up_number}:
- If #1: Brief, reference a different angle/problem than the first email. Add value.
- If #2: Very short, slightly more direct. Maybe share a quick stat or result.
- If #3: "Breakup" email — short, says you won't follow up again, leaves door open.

Start with "{greeting}" and keep it under 100 words.

Return as JSON with "subject" and "body" keys.
"""

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = response.content[0].text
    try:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        result = json.loads(text)
        return {"subject": result["subject"], "body": result["body"]}
    except (json.JSONDecodeError, KeyError):
        return {"subject": f"Re: {previous_email_subject}", "body": text}
