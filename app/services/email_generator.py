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


async def generate_linkedin_message(
    business_name: str,
    business_type: str,
    problems: list,
    contact_name: Optional[str] = None,
    message_type: str = "connect",  # connect or message
) -> dict:
    """
    Generate a LinkedIn connection request note or direct message.
    Returns dict with 'subject' (task title) and 'body' (the message).
    """
    first_name = _extract_first_name(contact_name)
    problems_context = json.dumps(problems[:2], indent=2)

    if message_type == "connect":
        user_prompt = f"""Write a LinkedIn connection request note for a prospect.

Business: {business_name}
Type: {business_type}
Contact first name: {first_name or "there"}

One problem we found:
{problems_context}

RULES:
- LinkedIn connection notes have a 300 character HARD LIMIT. Stay under 280 characters.
- Don't pitch. Just be curious/relevant. Reference something specific about their business.
- Make them want to accept. "Hey John, saw your pool work in Phoenix — impressive portfolio. Love to connect with fellow backyard pros."
- NO sign-off. NO "I'd love to". Just natural.

Return as JSON: {{"subject": "LinkedIn connect: {first_name or business_name}", "body": "..."}}
"""
    else:
        user_prompt = f"""Write a LinkedIn direct message to a prospect we're already connected with.

Business: {business_name}
Type: {business_type}
Contact first name: {first_name or "there"}

Problems we found on their website:
{problems_context}

RULES:
- Under 150 words. LinkedIn messages should be shorter than email.
- Reference a specific insight about their business (from the problems).
- CTA: offer something specific — "happy to send you a quick breakdown" or "want me to show you what [competitor] is doing differently?"
- Casual LinkedIn tone. Like you're messaging a connection, not writing an email.
- Start with "Hey {first_name}" — NO sign-off at the end.

Return as JSON: {{"subject": "LinkedIn message: {first_name or business_name}", "body": "..."}}
"""

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
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
        return {"subject": f"LinkedIn: {first_name or business_name}", "body": text}


IMESSAGE_SYSTEM_PROMPT = """You are writing a personalized iMessage for a BDR at Backyard Marketing Pros.
We help backyard professionals (pool builders, landscapers, deck builders, outdoor kitchen
builders) grow their business through marketing.

This is a TEXT MESSAGE, not an email. The recipient is going to read it on their phone in a
group of texts from their family, employees, and customers. It needs to feel like a real
person texting, not marketing copy.

CRITICAL RULES:

1. UNDER 240 CHARACTERS. Hard cap. Shorter is better — 100-180 chars is the sweet spot.

2. First name only. "Hey John" or "Hi John" — never the last name, never "Mr. Smith".
   If no name is known, just open with the message body, no greeting.

3. ONE specific personalization beat. Reference something concrete: a recent LinkedIn post
   they wrote, a specific problem on their site, a Google review they replied to. Don't say
   "I checked out your site" — that's everyone. Be specific in a way that proves you read
   their actual stuff.

4. ONE soft ask. "Worth a 5-min call?" or "Want me to send you what I found?" Never "schedule
   a demo" or "book a call" — too formal for a text.

5. NO signature, NO sign-off, NO "Best", NO "- Steve". Texts don't have signatures. The fact
   that it's coming from BMP's number is the signature.

6. NO emojis unless they're genuinely natural to the line. One 👀 or 🤔 max if it fits.

7. Casual punctuation. Lowercase is fine. Em dashes / commas are fine. Avoid semicolons —
   nobody texts with semicolons.

NEVER use:
- "I hope this finds you well" (it's a text, this makes no sense)
- "I'd love to" / "I'd be happy to" (too formal)
- "Are you the right person?"
- "I came across your business"
- Marketing words: "synergy", "leverage", "optimize", "solutions", "ROI", "scale"

TONE: Like a friend who works in marketing who noticed something while scrolling and
sent a quick text. Curious, specific, low-pressure.
"""


async def generate_imessage(
    business_name: str,
    business_type: str,
    contact_name: Optional[str] = None,
    problems: Optional[list] = None,
    recent_posts: Optional[list] = None,
    location: Optional[str] = None,
    intent: str = "intro",  # 'intro', 'follow_up', 'after_email'
) -> dict:
    """
    Generate a personalized iMessage. Returns {'body': str} — no subject because
    iMessages have no subject line.

    intent semantics:
      - 'intro': cold first-touch via iMessage (rare; usually after a call/email)
      - 'follow_up': nudge after an earlier message went unanswered
      - 'after_email': a "did my email get buried?" follow-up
    """
    first_name = _extract_first_name(contact_name)

    # Build personalization context — favor recent posts (LinkedIn), then fall back to problems
    context_lines: list[str] = []
    if recent_posts:
        for p in recent_posts[:2]:
            txt = (p.get("text") or "").strip()
            if txt:
                context_lines.append(f"- Recent post: {txt[:280]}")
    if problems:
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        sorted_problems = sorted(problems, key=lambda p: severity_order.get(p.get("severity", "low"), 3))
        for p in sorted_problems[:1]:
            title = p.get("title") or p.get("issue") or ""
            evidence = p.get("evidence") or p.get("description") or ""
            if title:
                context_lines.append(f"- Site problem: {title}{' — ' + evidence[:200] if evidence else ''}")
    context_block = "\n".join(context_lines) if context_lines else "(no specific personalization context — fall back to a curious general nudge)"

    intent_hint = {
        "intro": "First-touch via text. Be curious, not pitchy. Reference one specific thing about their business.",
        "follow_up": "They haven't responded yet. Keep it light. One short line. Don't restate the original ask.",
        "after_email": "You sent an email recently — this is the bump. Acknowledge that in a casual way (\"did the email I sent get buried?\" energy).",
    }.get(intent, "First-touch via text.")

    user_prompt = f"""Write an iMessage (TEXT MESSAGE) for this prospect:

Business: {business_name}
Type: {business_type}
Location: {location or "Unknown"}
Contact first name: {first_name or "(unknown — no greeting)"}
Intent: {intent} — {intent_hint}

Personalization context:
{context_block}

Return as JSON: {{"body": "the text message, under 240 chars, no signature"}}
"""

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=IMESSAGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = response.content[0].text
    try:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        result = json.loads(text)
        body = (result.get("body") or "").strip()
        # Strip any signature the model snuck in despite instructions
        for sign_off in ["Best,", "Thanks,", "Cheers,", "- ", "—Steve", "— Steve",
                         "Backyard Marketing", "BMP"]:
            lines = body.split("\n")
            while lines and lines[-1].strip().startswith(sign_off):
                lines.pop()
            body = "\n".join(lines).rstrip()
        return {"body": body, "char_count": len(body)}
    except (json.JSONDecodeError, KeyError):
        return {"body": text.strip(), "char_count": len(text.strip())}
