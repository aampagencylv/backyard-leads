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


SYSTEM_PROMPT = """You are writing cold outreach emails for a BDR at Backyard Marketing Pros.
We help backyard professionals (pool builders, landscapers, outdoor kitchen builders, deck builders)
grow their business through marketing.

CRITICAL RULES:

1. USE ONLY THE CONTACT'S FIRST NAME. Not "Hi John Smith" — just "Hi John". If no name, use "Hi".

2. DO NOT include any sign-off, signature, closing, or name at the end. No "Best," no "Thanks,"
   no "- Steve" no "Backyard Marketing Pros team". The email system adds a professional signature
   automatically. Your body should end with the last sentence of the message, nothing else.

3. Write like you're texting a colleague who owns a business, not writing a formal letter.
   Short sentences. Casual but smart. No fluff.

4. Reference ONE specific problem you found. Be concrete — use the actual data point
   (their site speed number, the specific missing feature, the exact SEO gap).

5. Keep it SHORT — under 120 words. Shorter emails get higher response rates.

6. The CTA should be soft and specific: "Want me to send you a quick breakdown?" or
   "Happy to show you what we did for [similar business]" — never "book a call" or "schedule a demo".

7. Subject lines: under 40 chars, lowercase feel, no clickbait, no emojis.

NEVER use:
- "I hope this email finds you well"
- "I'd love to" / "I'd be happy to" (too formal)
- "synergy" / "leverage" / "optimize" / "solutions"
- "Are you the right person?"
- "I came across your business" (everyone says this)
- Any greeting other than "Hi [FirstName]" or "Hey [FirstName]"

TONE: You've done your homework. You know their business. You spotted something they'd want to know about.
Like a friend who works in marketing mentioning something useful over a beer.
"""


def _extract_first_name(contact_name: Optional[str]) -> str:
    """Get just the first name from a full name string."""
    if not contact_name:
        return ""
    return contact_name.strip().split()[0]


async def generate_cold_email(
    business_name: str,
    business_type: str,
    website: str,
    problems: list,
    contact_name: Optional[str] = None,
    location: Optional[str] = None,
) -> dict:
    """
    Generate a personalized cold email based on problems found.
    Returns dict with 'subject' and 'body'.
    """
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    sorted_problems = sorted(problems, key=lambda p: severity_order.get(p.get("severity", "low"), 3))
    top_problems = sorted_problems[:3]

    problems_context = json.dumps(top_problems, indent=2)
    first_name = _extract_first_name(contact_name)
    greeting = f"Hi {first_name}" if first_name else "Hi"

    user_prompt = f"""Write a cold outreach email for this prospect:

Business: {business_name}
Type: {business_type}
Website: {website}
Location: {location or "Unknown"}
Contact first name: {first_name or "Unknown"}

Problems we found on their website (pick the most compelling one to lead with):
{problems_context}

Start with "{greeting}" — remember, NO sign-off at the end. The signature is added automatically.

Return as JSON: {{"subject": "...", "body": "..."}}
"""

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
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
        body = result["body"].rstrip()
        # Strip any trailing signature the model might add anyway
        for sign_off in ["Best,", "Thanks,", "Cheers,", "Talk soon,", "Best regards,",
                         "- ", "—", "Backyard Marketing", "BMP"]:
            lines = body.split("\n")
            while lines and lines[-1].strip().startswith(sign_off):
                lines.pop()
            body = "\n".join(lines).rstrip()
        return {"subject": result["subject"], "body": body}
    except (json.JSONDecodeError, KeyError):
        return {"subject": f"quick question about {business_name}", "body": text}


async def generate_follow_up(
    business_name: str,
    business_type: str,
    problems: list,
    previous_email_subject: str,
    follow_up_number: int = 1,
    contact_name: Optional[str] = None,
) -> dict:
    """Generate a follow-up email."""
    problems_context = json.dumps(problems[:3], indent=2)
    first_name = _extract_first_name(contact_name)
    greeting = f"Hi {first_name}" if first_name else "Hi"

    user_prompt = f"""Write follow-up #{follow_up_number} for this prospect who didn't respond to my first email.

Business: {business_name}
Type: {business_type}
Previous email subject: {previous_email_subject}
Contact first name: {first_name or "Unknown"}

Problems from their site:
{problems_context}

Rules for follow-up #{follow_up_number}:
- If #1: Brief, reference a different angle/problem than the first email. Add value — maybe share a quick insight.
- If #2: Very short (3-4 sentences max). More direct. Share a quick stat or result from a similar client.
- If #3: "Breakup" email — 2-3 sentences. Say you won't bug them again but leave the door open.

Start with "{greeting}" — NO sign-off at the end. Signature is automatic.
Keep it under 80 words.

Return as JSON: {{"subject": "...", "body": "..."}}
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
        body = result["body"].rstrip()
        for sign_off in ["Best,", "Thanks,", "Cheers,", "Talk soon,", "Best regards,",
                         "- ", "—", "Backyard Marketing", "BMP"]:
            lines = body.split("\n")
            while lines and lines[-1].strip().startswith(sign_off):
                lines.pop()
            body = "\n".join(lines).rstrip()
        return {"subject": result["subject"], "body": body}
    except (json.JSONDecodeError, KeyError):
        return {"subject": f"re: {previous_email_subject}", "body": text}
