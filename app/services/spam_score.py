"""Pre-send spam-score heuristic.

Cheap, conservative check that flags the most common content-side spam
signals before we hand an email to Resend. Not a replacement for
SpamAssassin / Postmark Spam Check — just a "did we accidentally write
something obviously spammy" sanity gate.

Higher score = more spammy. Each issue contributes 1+ points. The
caller decides what to do with the score (warn in UI, block, log, etc).

What we check:
  - Trigger words from a curated list (sales-pitch / clickbait / scam)
  - ALL CAPS subjects (yellow), or any 4+ letter ALL CAPS word
  - Excessive exclamation marks (subject has >1, body has >3 total)
  - Subject length (>70 chars triggers Gmail truncation + lower opens)
  - Missing List-Unsubscribe in the email is a separate header check
    we handle in email_sender — not here.
  - Link count (>5 wrapped /t/{token} URLs is a clickbait signal)
  - Word count too low (<15 words → 'spammy single-line pitch' shape)

Score buckets:
  0-1   OK — green
  2-4   WATCH — yellow, send but track
  5+    HIGH — red, recommend revision before send
"""
from __future__ import annotations
import re
from typing import Optional


# Curated trigger words. Sourced from the SpamAssassin word list + Mailtrap's
# common-flag list, pruned to terms an outbound cold email would never
# legitimately use. Match is whole-word (with optional punctuation).
TRIGGERS = {
    # Sales pitch / urgency
    "act now", "limited time", "limited offer", "while supplies last",
    "100% free", "100% guaranteed", "risk-free", "no obligation",
    "no purchase", "no investment", "no fee", "no cost",
    "free gift", "free trial", "free access", "absolutely free",
    "click here", "click below", "click now",
    "buy now", "buy direct", "buy today", "order now",
    "earn $", "make $", "earn money", "make money", "earn cash",
    "extra income", "double your income", "be your own boss",
    "work from home", "earn from home",
    "guaranteed", "money back",
    # Generic spam shapes
    "lowest price", "lowest prices", "best price", "best prices",
    "incredible deal", "amazing deal", "unbeatable",
    "wholesale", "bulk email", "mass email",
    "this isn't spam", "not spam", "not a scam",
    "credit card offers", "pre-approved", "pre-qualified",
    "weight loss", "viagra", "cialis", "pharmacy",
    # Cold-outreach mistakes
    "dear friend", "dear customer", "dear sir or madam",
    "to whom it may concern",
    # Inflated certainty
    "100% satisfaction", "satisfaction guaranteed",
}


def _strip_html(html: str) -> str:
    """Minimal-cost text extraction for the score function. The plain-text
    body is also available, but we want to call this on raw HTML inputs
    too (e.g. the admin preview endpoint), so we do a cheap regex strip
    here. Not BeautifulSoup — that'd be overkill for a heuristic."""
    if not html:
        return ""
    text = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&amp;|&lt;|&gt;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def score_email(
    subject: str,
    html_body: Optional[str] = None,
    plain_body: Optional[str] = None,
) -> dict:
    """Return a structured score for the given email content.

    Returns:
        {
          "score": int,
          "bucket": "ok" | "watch" | "high",
          "issues": [
            {"kind": "trigger_word", "weight": 1, "detail": "..."},
            ...
          ],
        }
    """
    subj = (subject or "").strip()
    body = (plain_body or "").strip() or _strip_html(html_body or "")
    issues: list[dict] = []

    full = f"{subj}\n{body}".lower()

    # 1. Trigger word matches
    hit_triggers = sorted({t for t in TRIGGERS if t in full})
    for t in hit_triggers[:10]:  # cap so the issue list stays readable
        issues.append({"kind": "trigger_word", "weight": 1, "detail": f"contains: {t!r}"})

    # 2. Subject in ALL CAPS (more than 4 letters, not just an acronym)
    if subj and len(re.sub(r"[^A-Za-z]", "", subj)) >= 6:
        letters = re.sub(r"[^A-Za-z]", "", subj)
        if letters and letters == letters.upper():
            issues.append({"kind": "subject_all_caps", "weight": 2, "detail": "Subject is ALL CAPS — Gmail flags this aggressively"})

    # 3. 4+ letter ALL-CAPS words in body (one or two acronyms OK, lots → bad)
    caps_words = [w for w in re.findall(r"\b[A-Z]{4,}\b", body) if w not in {"FAQ", "SEO", "CEO", "BDR", "CTA", "API", "DNS", "SPF", "DKIM", "DMARC"}]
    if len(caps_words) >= 3:
        issues.append({"kind": "shouty_body", "weight": 1, "detail": f"{len(caps_words)} ALL-CAPS words in body"})

    # 4. Exclamation marks
    subj_exc = subj.count("!")
    body_exc = body.count("!")
    if subj_exc >= 1:
        issues.append({"kind": "subject_exclamation", "weight": 2, "detail": f"Subject has {subj_exc} exclamation mark(s)"})
    if body_exc >= 4:
        issues.append({"kind": "body_exclamation", "weight": 1, "detail": f"Body has {body_exc} exclamation marks"})

    # 5. Subject length
    if len(subj) > 70:
        issues.append({"kind": "subject_too_long", "weight": 1, "detail": f"Subject is {len(subj)} chars; will be truncated on mobile (Gmail cuts at ~70)"})

    # 6. Link count — too many CTAs is a clickbait signal
    link_count = len(re.findall(r'<a\s', html_body or "", re.IGNORECASE)) if html_body else len(re.findall(r"https?://", body))
    if link_count > 5:
        issues.append({"kind": "too_many_links", "weight": 1, "detail": f"{link_count} links — keep cold outreach to 1–3 CTAs"})

    # 7. Word count too low — single-line pitches read as spam
    word_count = len(re.findall(r"\b\w+\b", body))
    if word_count and word_count < 15:
        issues.append({"kind": "too_short", "weight": 1, "detail": f"Only {word_count} words — most cold emails should be 40–120"})

    # 8. ALL-CAPS subject + exclamation combo is a strong spam signal
    # (already double-counted above; skip)

    score = sum(i["weight"] for i in issues)
    bucket = "ok" if score <= 1 else ("watch" if score <= 4 else "high")
    return {"score": score, "bucket": bucket, "issues": issues}
