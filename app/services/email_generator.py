"""
AI Email Generator Service
Uses Claude to generate personalized outreach emails based on
specific marketing problems found on the prospect's website.
"""
from __future__ import annotations
import json
import logging as _logging
import re
from typing import Optional
from app.config import settings
from app.services.ai_client import chat_with_system, MODEL_BALANCED

_log = _logging.getLogger("bmp.email_generator")


class EmailGenerationError(Exception):
    """Raised when the model output can't be cleanly parsed into
    subject + body. Callers should treat this as 'skip this send +
    schedule a retry' — NEVER fall back to sending raw model output
    as the email (catastrophic prod incident 2026-06-09: 10 prospects
    received raw JSON in their inbox over 4 days)."""


def _parse_email_response(text: str) -> dict:
    """Robustly extract {'subject', 'body'} from a Claude response.

    Three layers, ordered most-strict → most-lenient:
      1. strict json.loads (works ~95% of the time)
      2. regex extraction (works when the body contains unescaped
         inner quotes that broke json.loads — e.g. Claude embedding
         a quoted phrase like 'people search "best plumber near me"')
      3. raise EmailGenerationError — DO NOT return raw text as body.

    The third layer is the contract: if we cannot prove we extracted
    a real subject + body, we refuse to produce an email rather than
    sending model garbage to a real prospect.
    """
    if not text or not text.strip():
        raise EmailGenerationError("empty model response")

    # Strip code fences
    s = text
    if "```json" in s:
        s = s.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in s:
        s = s.split("```", 1)[1].split("```", 1)[0]
    s = s.strip()

    # Layer 1: strict JSON
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and "subject" in obj and "body" in obj:
            subj = (obj["subject"] or "").strip()
            body = (obj["body"] or "").rstrip()
            if subj and body:
                return {"subject": subj, "body": body}
    except json.JSONDecodeError:
        pass

    # Layer 2: regex extraction tolerant of unescaped inner quotes
    # Subject: first quoted run after "subject":
    subj_m = re.search(r'"subject"\s*:\s*"([^"\n]+)"', s)
    # Body: from after "body": " until the LAST " before EOS.
    # Trailing pattern allows arbitrary whitespace, commas, and closing
    # braces (Claude sometimes emits trailing `}` then `}}` etc. — caught
    # action #1954 where the model produced ...body": "..."\n  }\n}).
    body_m = re.search(
        r'"body"\s*:\s*"(.+)"\s*[\r\n,}\s]*\Z',
        s, flags=re.DOTALL,
    )
    if subj_m and body_m:
        subj = subj_m.group(1).strip()
        # Unescape standard JSON-string escapes the model produced
        body = body_m.group(1)
        body = (body
                .replace(r"\n", "\n")
                .replace(r"\t", "\t")
                .replace(r"\"", '"')
                .replace(r"\\", "\\"))
        body = body.rstrip()
        if subj and body:
            _log.warning(
                "email_generator: JSON parse failed but regex recovered "
                "(likely unescaped inner quotes in model body)"
            )
            return {"subject": subj, "body": body}

    # Layer 3: refuse to produce garbage. Caller should retry or skip.
    preview = (text or "")[:160].replace("\n", " ")
    raise EmailGenerationError(
        f"cannot parse subject+body from model response (first 160 chars: {preview!r})"
    )


def _strip_signature(body: str) -> str:
    """Drop any trailing sign-off the model may have added despite the
    system prompt's instruction not to. The sender_signature is appended
    by email_sender at send time, so anything model-generated here is
    a duplicate."""
    for sign_off in ["Best,", "Thanks,", "Cheers,", "Talk soon,",
                     "Best regards,", "- ", "—", "Backyard Marketing", "BMP"]:
        lines = body.split("\n")
        while lines and lines[-1].strip().startswith(sign_off):
            lines.pop()
        body = "\n".join(lines).rstrip()
    return body


# Friendly anchor text for the AI-findability audit CTA. Stored in the
# body as a markdown link `[CTA](url)` so the prospect sees a clickable
# phrase (never a raw URL); wrap_html_links / send_email render + track it.
AUDIT_CTA_TEXT = "View Your AI Visibility Report"


def inject_audit_cta(body: str, audit_url: Optional[str]) -> str:
    """Ensure the audit link appears as a friendly markdown CTA, never a
    raw URL. Three layers, in priority order:
      1. Replace the {{AUDIT_LINK}} placeholder the prompt asks for.
      2. Failing that, replace any raw occurrence of the audit URL.
      3. Failing that (model dropped it entirely), append the CTA.
    No-op when audit_url is falsy. Idempotent — if the markdown link is
    already present it won't double-inject."""
    if not audit_url:
        # Still strip a stray placeholder so it never ships literally.
        return (body or "").replace("{{AUDIT_LINK}}", "").rstrip()
    md = f"[{AUDIT_CTA_TEXT}]({audit_url})"
    if md in (body or ""):
        return body
    if "{{AUDIT_LINK}}" in body:
        return body.replace("{{AUDIT_LINK}}", md)
    if audit_url in body:
        return body.replace(audit_url, md)
    return body.rstrip() + f"\n\n{md}"


SYSTEM_PROMPT = """You are writing cold outreach emails for a BDR at a B2B marketing agency.
The agency's specific focus, industry, and value proposition are described in
the STRATEGIC DIRECTION section above (every prospect message you write should
reflect those specifics — the rules below are channel-format only).

CRITICAL RULES:

1. USE ONLY THE CONTACT'S FIRST NAME. Not "Hi John Smith" — just "Hi John". If no name, use "Hi".

2. DO NOT include any sign-off, signature, closing, or name at the end. No "Best," no "Thanks,"
   no "- [Name]" no "[Agency] team". The email system adds a professional signature
   automatically. Your body should end with the last sentence of the message, nothing else.

3. Write like you're texting a colleague who owns a business, not writing a formal letter.
   Short sentences. Casual but smart. No fluff.

4. Reference ONE specific problem you found. Be concrete — use the actual data point
   (their site speed number, the specific missing feature, the exact SEO gap).

5. Keep it SHORT — under 120 words. Shorter emails get higher response rates.

6. The CTA should be soft and specific: "Want me to send you a quick breakdown?" or
   "Happy to show you what we did for [similar business]" — never "book a call" or "schedule a demo".

7. Subject lines: under 40 chars, natural sentence case (capitalize the first word
   and proper nouns; lowercase everything else). No clickbait, no emojis, no
   ALL CAPS, no Title Case With Every Word Capitalized — that screams marketing.

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


def _compose_system_prompt(base_prompt: str, messaging_direction: Optional[str]) -> str:
    """Prepend the org's messaging direction to a channel-specific system prompt.
    The direction sets the strategic angle (what we lead with — e.g. AI findability /
    GEO / local SEO); the channel prompt sets the format constraints (length, tone,
    sign-off rules). Both compose without conflicting."""
    if not messaging_direction or not messaging_direction.strip():
        return base_prompt
    return f"=== STRATEGIC DIRECTION (apply across all channels) ===\n\n{messaging_direction.strip()}\n\n=== CHANNEL FORMAT RULES ===\n\n{base_prompt}"


async def generate_cold_email(
    business_name: str,
    business_type: str,
    website: str,
    problems: list,
    contact_name: Optional[str] = None,
    location: Optional[str] = None,
    messaging_direction: Optional[str] = None,
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

    # The composed system prompt is large (~1500 tokens) and fixed across
    # all cold-email generations within a campaign — perfect for prompt
    # caching. The first call pays full input price; subsequent calls
    # within ~5 min pay 10% on the cached prefix, which is ~5-10x cheaper
    # on the dominant cost in this code path.
    text = await chat_with_system(
        model=MODEL_BALANCED,
        system=_compose_system_prompt(SYSTEM_PROMPT, messaging_direction),
        user=user_prompt,
        max_tokens=500,
        cacheable=True,
    )
    from app.services.credit_meter import meter_standalone as _meter_ai
    await _meter_ai(action_type="ai_email_gen", action_ref=f"cold_email:{business_name[:60]}",
                    metadata={"max_tokens": 500, "kind": "cold_email"})
    # Parse model output robustly. _parse_email_response raises
    # EmailGenerationError if it can't extract a real subject+body —
    # better to skip the send than to ship raw JSON to a prospect.
    result = _parse_email_response(text)
    return {
        "subject": result["subject"],
        "body": _strip_signature(result["body"]),
    }


async def generate_follow_up(
    business_name: str,
    business_type: str,
    problems: list,
    previous_email_subject: str,
    follow_up_number: int = 1,
    contact_name: Optional[str] = None,
    messaging_direction: Optional[str] = None,
    audit_url: Optional[str] = None,
) -> dict:
    """Generate a follow-up email.

    When `audit_url` is provided, the AI is instructed to share the
    pre-run AI findability audit link as the value-add — that's the
    natural hook for follow-up #1 ('I went ahead and ran an analysis
    — here's what stood out: <url>'). Follow-up #1 is the right place
    for the link by default; #2 references the audit if useful but
    doesn't have to drop the link again."""
    problems_context = json.dumps(problems[:3], indent=2)
    first_name = _extract_first_name(contact_name)
    greeting = f"Hi {first_name}" if first_name else "Hi"

    audit_clause = ""
    if audit_url and follow_up_number in (1, 2):
        audit_clause = (
            f"\n\nIMPORTANT: We've already run an AI Findability audit on their site. "
            f"For follow-up #1, write a short value-add lead-in, then on its OWN LINE "
            f"put the literal token {{{{AUDIT_LINK}}}} — it becomes a clickable button "
            f"that opens their report. Do NOT paste a raw URL; use the token. Example:\n"
            f"  'I actually went ahead and ran a quick AI findability scan on your site:\\n\\n"
            f"{{{{AUDIT_LINK}}}}\\n\\nTakes two minutes to look through.'\n"
            f"For follow-up #2, briefly reference the audit (e.g. 'the analysis I "
            f"shared') WITHOUT the token — don't drop the link again."
        )

    user_prompt = f"""Write follow-up #{follow_up_number} for this prospect who didn't respond to my first email.

Business: {business_name}
Type: {business_type}
Previous email subject: {previous_email_subject}
Contact first name: {first_name or "Unknown"}

Problems from their site:
{problems_context}
{audit_clause}

Rules for follow-up #{follow_up_number}:
- If #1: Brief, reference a different angle/problem than the first email. Add value — maybe share a quick insight.
- If #2: Very short (3-4 sentences max). More direct. Share a quick stat or result from a similar client.
- If #3: "Breakup" email — 2-3 sentences. Say you won't bug them again but leave the door open.

Start with "{greeting}" — NO sign-off at the end. Signature is automatic.
Keep it under {120 if audit_url and follow_up_number == 1 else 80} words.

Return as JSON: {{"subject": "...", "body": "..."}}
"""

    text = await chat_with_system(
        model=MODEL_BALANCED,
        system=_compose_system_prompt(SYSTEM_PROMPT, messaging_direction),
        user=user_prompt,
        max_tokens=400,
        cacheable=True,
    )
    from app.services.credit_meter import meter_standalone as _meter_ai
    await _meter_ai(action_type="ai_email_gen", action_ref=f"follow_up:{business_name[:60]}",
                    metadata={"max_tokens": 400, "kind": "follow_up"})

    result = _parse_email_response(text)
    body = _strip_signature(result["body"])
    # Normalize the audit link into a friendly markdown CTA on follow-up
    # #1 (the step that carries the link). For every other step pass None
    # so a stray {{AUDIT_LINK}} placeholder is stripped rather than
    # turned into an appended CTA.
    body = inject_audit_cta(body, audit_url if follow_up_number == 1 else None)
    return {
        "subject": result["subject"],
        "body": body,
    }


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

    text = await chat_with_system(
        model=MODEL_BALANCED,
        system=SYSTEM_PROMPT,
        user=user_prompt,
        max_tokens=300,
        cacheable=True,
    )
    from app.services.credit_meter import meter_standalone as _meter_ai
    await _meter_ai(action_type="ai_email_gen", action_ref=f"linkedin:{business_name[:60]}",
                    metadata={"max_tokens": 300, "kind": "linkedin_message"})

    result = _parse_email_response(text)
    return {"subject": result["subject"], "body": result["body"]}


IMESSAGE_SYSTEM_PROMPT = """You are writing a personalized iMessage for a BDR at a B2B marketing
agency. The agency's specific focus, industry, and value proposition are described in
the STRATEGIC DIRECTION section above (lean on those specifics — the rules below are
channel-format only).

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
    messaging_direction: Optional[str] = None,
    audit_url: Optional[str] = None,
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

    audit_clause = ""
    if audit_url:
        audit_clause = (
            f"\n\nIMPORTANT: Include this audit link in the message naturally:\n"
            f"  {audit_url}\n"
            f"Phrasing should feel value-add. Example: 'Ran a quick AI scan on "
            f"{business_name} — 3 things stood out, posted it here: {audit_url}'. "
            f"Keep it casual — texts shouldn't sound like emails."
        )

    user_prompt = f"""Write an iMessage (TEXT MESSAGE) for this prospect:

Business: {business_name}
Type: {business_type}
Location: {location or "Unknown"}
Contact first name: {first_name or "(unknown — no greeting)"}
Intent: {intent} — {intent_hint}

Personalization context:
{context_block}
{audit_clause}

Return as JSON: {{"body": "the text message, under {280 if audit_url else 240} chars, no signature"}}
"""

    text = await chat_with_system(
        model=MODEL_BALANCED,
        system=_compose_system_prompt(IMESSAGE_SYSTEM_PROMPT, messaging_direction),
        user=user_prompt,
        max_tokens=300,
        cacheable=True,
    )
    from app.services.credit_meter import meter_standalone as _meter_ai
    await _meter_ai(action_type="ai_email_gen", action_ref=f"imessage:{business_name[:60]}",
                    metadata={"max_tokens": 300, "kind": "imessage"})

    try:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        result = json.loads(text)
        body = (result.get("body") or "").strip()
        body = _strip_signature(body)
        if body:
            return {"body": body, "char_count": len(body)}
        # Empty body extracted — fall through to regex
        raise json.JSONDecodeError("empty body in parsed JSON", text, 0)
    except (json.JSONDecodeError, KeyError):
        # Try regex extraction for body when JSON parsing failed (e.g.
        # inner quotes broke the parse) BEFORE falling back to raw text.
        # Same defense as _parse_email_response — never send raw model
        # garbage to a real prospect.
        body_m = re.search(
            r'"body"\s*:\s*"(.+)"\s*[\r\n,]*\s*\}?\s*\Z',
            text, flags=re.DOTALL,
        )
        if body_m:
            body = (body_m.group(1)
                    .replace(r"\n", "\n").replace(r"\t", "\t")
                    .replace(r"\"", '"').replace(r"\\", "\\"))
            body = _strip_signature(body.rstrip())
            if body:
                _log.warning("imessage: regex recovered body after JSON parse failure")
                return {"body": body, "char_count": len(body)}
        # Previously we accepted "plain text that doesn't look like JSON"
        # as a valid body — but Claude often returns preamble like
        # "Sure! Here's a casual iMessage for John:\n\nHey John..."
        # which doesn't start with `{` and doesn't contain `"body"`,
        # so it would pass through and that ENTIRE response (preamble
        # included) would land on the prospect's phone via Blooio.
        # Blooio has NO anomaly guard analog to email_sender, so the
        # only defense is here. Raise instead of accepting plain text.
        raise EmailGenerationError(
            f"imessage: cannot extract a body field from model response "
            f"(first 160 chars: {(text or '')[:160]!r})"
        )


# ============================================================
# Post-call sequence generator
#
# Reads the call transcript + Claude takeaways and drafts a 3-step follow-up:
#   Step 1 (~2 hr after the call): Thank-you email referencing 2-3 concrete things
#                                   they discussed
#   Step 2 (Day 2): iMessage bump if they haven't replied to Step 1
#   Step 3 (Day 5): Calendar nudge with 2-3 specific time options
#
# Each step is highly personalized — not "thanks for our call", but "thanks
# for walking me through how the new pool spec form has been hurting your
# close rate; here's a quick mockup of what I was sketching".
# ============================================================

POST_CALL_SYSTEM_PROMPT = """You are writing a 3-step follow-up sequence after a sales discovery call.
The BDR is at a B2B marketing agency; the agency's specific focus is described in the
STRATEGIC DIRECTION section above. The recipient is a prospect who just spoke with the
BDR on the phone.

You will be given:
  - The call transcript (with diarized speaker labels)
  - A pre-generated AI summary of the call (the "takeaways")

Your job: write 3 distinct follow-up touches that build on the actual conversation.

CRITICAL RULES (every step):

1. Reference SPECIFIC THINGS they said on the call. Not generic recap. If they
   mentioned a competitor, a frustration, a metric, a goal — quote it back.
   Show you were listening.

2. Use first name only. If no name is known, omit the greeting.

3. NO sign-off. Email signatures are added automatically; iMessages don't have them.

4. Match the tone of the call. If they were casual, be casual. If they were
   professional and reserved, mirror that.

PER-STEP SHAPE:

Step 1 — Thank-you email (sent ~2 hours after the call):
  - Subject: short and specific to what was discussed (e.g. "the lead-form thing
    you mentioned" — NOT "great talking with you")
  - Body: 80-120 words. Open with one specific thing from the call. Either:
    (a) deliver something concrete they asked for ("here's that pricing breakdown
        for the silver package")
    (b) recap the agreed-upon next step ("you said you'd loop in your operations
        manager — happy to send a calendar invite when you're ready")
    (c) share a relevant insight you didn't get to on the call

Step 2 — iMessage bump (sent Day 2 if no reply):
  - Under 200 chars. Casual text vibe.
  - One short line referencing the call without restating the entire ask.
  - Example: "Hey John — just bumping the email from Tuesday in case it got
    buried. Did you get a chance to look at the pricing?"

Step 3 — Calendar nudge (sent Day 5 if still no reply):
  - Subject: super specific. "next steps on the website rebuild" not "checking in"
  - Body: 60-90 words. Acknowledge the lag without being passive-aggressive.
    Offer 2-3 specific time options ("Tuesday 10am, Wednesday 2pm, or Thursday
    3pm any work?"). Make it easy to say yes.

NEVER:
- "I hope you're well"
- "Just following up"
- "Wanted to circle back"
- "Touching base"
- Re-introducing yourself or restating who BMP is — they just spent 30 min on
  the phone with you, they know.
"""


async def generate_post_call_sequence(
    business_name: str,
    business_type: str,
    contact_name: Optional[str],
    transcript: str,
    summary: Optional[str] = None,
    duration_seconds: Optional[int] = None,
    messaging_direction: Optional[str] = None,
) -> list[dict]:
    """Returns a list of 3 step dicts, each with: step_type, day, subject,
    body, channel-appropriate fields. Caller wraps these into GeneratedEmail
    rows under sequence_label='post_call'.

    If transcript is short or empty, falls back to a generic 3-step template
    rather than failing — the BDR can edit before the steps fire.
    """
    first_name = _extract_first_name(contact_name)
    transcript = (transcript or "").strip()

    # Truncate transcript at ~6000 chars to stay within reasonable token budget.
    # Diarized transcripts can run long; the first ~6k chars usually capture
    # the meat of a 30-min call.
    if len(transcript) > 6000:
        transcript = transcript[:6000] + "\n\n[transcript truncated for length]"

    # Fallback if no transcript — return generic shells the BDR can edit
    if len(transcript) < 200:
        return [
            {
                "step_type": "email", "day": 0,
                "subject": f"following up on our call",
                "body": f"Hi {first_name or 'there'} — quick follow-up from our chat earlier. Wanted to make sure I have everything I need on my end. Let me know if anything else came to mind.",
            },
            {
                "step_type": "imessage", "day": 2,
                "subject": "iMessage bump (post-call)",
                "body": f"Hey {first_name or 'there'} — just bumping my email from a couple days ago. Got a sec?",
            },
            {
                "step_type": "email", "day": 5,
                "subject": "next steps?",
                "body": f"Hey {first_name or 'there'} — wanted to see if you had a chance to think on what we discussed. Happy to grab another quick call if that's easier — Tuesday 10am, Wednesday 2pm, or Thursday 3pm any work?",
            },
        ]

    duration_min = round((duration_seconds or 0) / 60)
    summary_block = f"\nAI takeaways from the call:\n{summary}\n" if summary else ""

    user_prompt = f"""Write a 3-step follow-up sequence after this call.

Business: {business_name}
Type: {business_type}
Contact first name: {first_name or "(unknown)"}
Call duration: {duration_min} minutes
{summary_block}
Call transcript (diarized):
{transcript}

Return JSON only, no other text:
{{
  "step1_email_subject": "...",
  "step1_email_body": "...",
  "step2_imessage_body": "...",
  "step3_email_subject": "...",
  "step3_email_body": "..."
}}
"""

    text = await chat_with_system(
        model=MODEL_BALANCED,
        system=_compose_system_prompt(POST_CALL_SYSTEM_PROMPT, messaging_direction),
        user=user_prompt,
        max_tokens=2000,
        cacheable=True,
    )
    from app.services.credit_meter import meter_standalone as _meter_ai
    # Post-call sequence is ~4x bigger output than a normal cold email,
    # so override the rate-card raw cost to reflect the real spend.
    await _meter_ai(action_type="ai_email_gen", action_ref=f"post_call_seq",
                    raw_cost_override_usd=0.018,
                    metadata={"max_tokens": 2000, "kind": "post_call_sequence"})

    try:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        r = json.loads(text)
        return [
            {
                "step_type": "email", "day": 0,
                "subject": (r.get("step1_email_subject") or "following up").strip(),
                "body":    (r.get("step1_email_body") or "").strip(),
            },
            {
                "step_type": "imessage", "day": 2,
                "subject": "iMessage bump (post-call)",
                "body":    (r.get("step2_imessage_body") or "").strip(),
            },
            {
                "step_type": "email", "day": 5,
                "subject": (r.get("step3_email_subject") or "next steps?").strip(),
                "body":    (r.get("step3_email_body") or "").strip(),
            },
        ]
    except (json.JSONDecodeError, KeyError, AttributeError):
        # Parse failed — DO NOT ship raw model output as the post-call
        # email body. The previous version returned `text[:1000]` which
        # could ship 1000 chars of Claude prose (preamble like
        # "Here's a 3-step sequence:\n\nStep 1 subject: ..." straight
        # to a prospect who just spent 30 minutes on a discovery call
        # — the highest-trust touchpoint in the funnel.
        # Raise so the route can 502 + the BDR can retry. The other
        # two shell steps are static text (no model output, no
        # BDR-typed leakage) so they could safely ship — but for
        # consistency we raise and let the BDR own the retry.
        raise EmailGenerationError(
            f"post_call_sequence: cannot parse 3-step JSON from model "
            f"response (first 160 chars: {(text or '')[:160]!r})"
        )


REWORK_SYSTEM_PROMPT = """You are a sales copywriter for a marketing agency. You're rewriting a prospect's
remaining outreach sequence based on a real conversation that just happened.

The BDR spoke with the prospect and now the follow-ups need to reflect what was discussed,
their objections, their timeline, and what they said they needed. Generic "just checking in"
messages won't cut it — each step should reference something specific from the conversation.

Rules:
- First-name only, casual professional tone
- No sign-off lines (no "Best," no "Cheers,")
- Reference specific things from the call notes/transcript
- Each step should move the deal forward, not just "touch base"
- Mix channels: email for detailed follow-ups, iMessage for quick nudges, calls for closing
- If the prospect gave a timeline ("call me in 2 weeks"), respect that in the spacing
"""


async def generate_reworked_sequence(
    business_name: str,
    business_type: str,
    contact_name: Optional[str],
    call_notes: str,
    transcript: Optional[str] = None,
    summary: Optional[str] = None,
    remaining_step_count: int = 5,
    messaging_direction: Optional[str] = None,
) -> list[dict]:
    """Generate a reworked follow-up sequence based on call context.
    Returns a list of step dicts with: step_type, day, subject, body."""
    first_name = _extract_first_name(contact_name)

    transcript_block = ""
    if transcript and len(transcript.strip()) > 100:
        t = transcript.strip()
        if len(t) > 4000:
            t = t[:4000] + "\n[truncated]"
        transcript_block = f"\nCall transcript:\n{t}\n"

    summary_block = f"\nAI call summary:\n{summary}\n" if summary else ""

    user_prompt = f"""Rewrite this prospect's follow-up sequence based on our actual conversation.

Business: {business_name}
Type: {business_type}
Contact: {first_name or "(unknown)"}

BDR's call notes:
{call_notes}
{summary_block}{transcript_block}
Generate {remaining_step_count} follow-up steps. Mix of email, iMessage, and call.
Space them out appropriately based on what the prospect said about timing.

Return JSON only, no other text. Array of objects:
[
  {{"step_type": "email"|"imessage"|"call", "day": <days_from_now>, "subject": "...", "body": "..."}}
]
"""

    text = await chat_with_system(
        model=MODEL_BALANCED,
        system=_compose_system_prompt(REWORK_SYSTEM_PROMPT, messaging_direction),
        user=user_prompt,
        max_tokens=3000,
        cacheable=True,
    )
    from app.services.credit_meter import meter_standalone as _meter_ai
    await _meter_ai(action_type="ai_email_gen", action_ref="rework_sequence",
                    raw_cost_override_usd=0.02,
                    metadata={"max_tokens": 3000, "kind": "rework_sequence"})

    try:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        steps = json.loads(text)
        if not isinstance(steps, list):
            raise ValueError("Expected a JSON array")
        result = []
        for s in steps:
            result.append({
                "step_type": s.get("step_type", "email"),
                "day": int(s.get("day", 0)),
                "subject": (s.get("subject") or "follow-up").strip(),
                "body": (s.get("body") or "").strip(),
            })
        return result
    except (json.JSONDecodeError, KeyError, AttributeError, ValueError):
        # DO NOT interpolate `call_notes` into the prospect-facing body.
        # The BDR's notes are internal — e.g. "client was rude, no real
        # budget" — and would leak straight into the email if the model
        # output failed to parse. Raise so the route surfaces an error
        # and the BDR can rewrite or retry. The static iMessage / call-
        # task shells in the original fallback were safe, but for the
        # email step the call_notes leak is unacceptable; raise wholesale.
        raise EmailGenerationError(
            f"reworked_sequence: cannot parse step list from model "
            f"response (first 160 chars: {(text or '')[:160]!r})"
        )
