"""
Email click-tracking — Phase 1 of Website Visitor Tracking.

Wraps every <a href="..."> in an outgoing email through a /t/{token} redirect.
When the prospect clicks, /t/{token} logs an Activity (type='email_clicked'),
drops the bmp_visitor cookie (Phase 2 will use it for cross-page session
tracking), and 302s to the original URL.

Skips wrapping for:
  - Anchor links (#section)
  - mailto:, tel:, sms: schemes
  - Unsubscribe links (must remain compliant first-party links per CAN-SPAM)
  - URLs already pointing at our domain (already trackable server-side)
"""
from __future__ import annotations
import re
import secrets
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TrackingLink
from app.config import settings


# Match every href="..." or href='...' in HTML, capturing the URL.
# Doesn't try to be a full HTML parser — emails use a small subset and this
# regex handles them reliably without pulling in a parser dep.
_HREF_RE = re.compile(r'''(href\s*=\s*)(["'])([^"']+)(["'])''', re.IGNORECASE)

# Match markdown-style links [display text](https://url). AI-generated
# bodies now emit the audit CTA as `[View Your AI Visibility Report](url)`
# so the prospect sees a friendly clickable phrase, never a raw URL.
# Converting to an <a> here (before the href pass) means the resulting
# anchor's href gets click-tracked while the display text is preserved.
_MD_LINK_RE = re.compile(r'\[([^\]\n]{1,120})\]\((https?://[^)\s]+)\)')


def linkify_markdown(text: str) -> str:
    """Convert markdown links [text](url) → <a href="url">text</a>.

    Pure string transform, no DB. Safe to run repeatedly (idempotent:
    once converted to an anchor there's no markdown left to match) and
    safe on plain prose (the `](http` shape doesn't occur naturally).
    Used both here (so the href becomes click-tracked) and as a
    last-resort render inside email_sender.send_email (so the CTA still
    renders as a real link even if click-wrapping is skipped)."""
    if not text or "](" not in text:
        return text
    return _MD_LINK_RE.sub(
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text
    )

# Match bare URLs in plain-text content (NOT already inside an href
# attribute or anchor tag). AI-generated email bodies are plain text —
# "posted the results here: https://audit.../report/xyz" — so without
# bare-URL wrapping, body links (including the audit-report link, the
# single most important CTA in the whole pitch) were NEVER click-tracked.
# Trailing-punctuation chars are excluded so "...report/xyz." doesn't
# swallow the period.
_BARE_URL_RE = re.compile(r'https?://[^\s<>"\')\]]+[^\s<>"\')\].,;:!?]')

UNTRACKABLE_PREFIXES = ("#", "mailto:", "tel:", "sms:", "javascript:")
SKIP_PATH_PATTERNS = ("/unsubscribe", "/u/")  # CAN-SPAM compliance — don't wrap unsubscribe links


def _should_track(url: str) -> bool:
    if not url:
        return False
    lower = url.strip().lower()
    if any(lower.startswith(p) for p in UNTRACKABLE_PREFIXES):
        return False
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False
    # Don't wrap our own tracking links (avoids double-wrap on regen)
    public_host = urlparse(settings.public_url).netloc
    if parsed.netloc == public_host and parsed.path.startswith("/t/"):
        return False
    if any(p in (parsed.path or "").lower() for p in SKIP_PATH_PATTERNS):
        return False
    return True


def _new_token() -> str:
    """22 chars of url-safe base64 entropy. Enough to make brute-force
    impractical without making URLs uglier than necessary."""
    return secrets.token_urlsafe(16)


async def wrap_html_links(
    db: AsyncSession,
    html: str,
    *,
    contact_id: Optional[int],
    company_id: Optional[int],
    email_id: Optional[int],
    label: str = "body_link",
    linkify_bare_urls: bool = True,
) -> str:
    """Replace every trackable href in `html` with a /t/{token} URL,
    AND (when linkify_bare_urls=True) convert bare plain-text URLs into
    tracked <a> anchors. Mints one TrackingLink row per URL. Commits in
    batches at the end — caller can wrap in their own transaction if
    needed; this commits to keep tokens persistent in case the email
    send itself fails afterwards (better to have orphan tokens than to
    drop tracking on a partial send).

    The bare-URL pass exists because AI-generated bodies are plain text:
    every body link (including the audit-report CTA) shipped as a bare
    URL that email clients auto-link client-side, invisible to us. The
    rewritten anchor keeps the original URL as the display text so the
    prospect sees what they expect; the href routes through /t/{token}.

    Returns the rewritten HTML. If no trackable links are found, returns
    the input unchanged (no DB writes)."""
    if not html:
        return html

    # Pass 0: markdown links [text](url) → <a href="url">text</a>. Run
    # first so the resulting anchor's href is click-tracked by the href
    # pass below, while the friendly display text is preserved.
    html = linkify_markdown(html)

    public_base = settings.public_url.rstrip("/")
    minted: list[tuple[int, int, str]] = []  # (start, end, replacement)
    rows_to_add: list[TrackingLink] = []

    # Pass 1: existing href attributes (signature links, any HTML bodies).
    href_spans: list[tuple[int, int]] = []
    for m in _HREF_RE.finditer(html):
        href_spans.append((m.start(), m.end()))
        attr_prefix = m.group(1)  # 'href='
        quote = m.group(2)
        url = m.group(3).strip()
        if not _should_track(url):
            continue
        token = _new_token()
        wrapped = f'{attr_prefix}{quote}{public_base}/t/{token}{quote}'
        minted.append((m.start(), m.end(), wrapped))
        rows_to_add.append(TrackingLink(
            token=token,
            contact_id=contact_id,
            company_id=company_id,
            email_id=email_id,
            destination_url=url,
            label=label,
        ))

    # Pass 2: bare plain-text URLs → tracked anchors. Skip any URL that
    # sits inside an href attribute span (already handled above) or
    # inside an existing anchor's display text (rewriting the display
    # text of an <a> would double-link).
    if linkify_bare_urls:
        anchor_spans = [
            (m.start(), m.end())
            for m in re.finditer(r"<a\b[^>]*>.*?</a>", html,
                                 flags=re.IGNORECASE | re.DOTALL)
        ]
        protected = href_spans + anchor_spans

        def _inside_protected(start: int, end: int) -> bool:
            return any(ps <= start and end <= pe for ps, pe in protected)

        for m in _BARE_URL_RE.finditer(html):
            if _inside_protected(m.start(), m.end()):
                continue
            url = m.group(0)
            if not _should_track(url):
                continue
            token = _new_token()
            # Display text stays the original URL so the prospect sees
            # exactly what the copy promised; only the href is tracked.
            wrapped = f'<a href="{public_base}/t/{token}">{url}</a>'
            minted.append((m.start(), m.end(), wrapped))
            rows_to_add.append(TrackingLink(
                token=token,
                contact_id=contact_id,
                company_id=company_id,
                email_id=email_id,
                destination_url=url,
                label=label,
            ))

    if not minted:
        return html

    # Substitutions must apply in reverse positional order; sort by start.
    minted.sort(key=lambda t: t[0])

    # Persist tokens BEFORE rewriting — if anything blows up, we'd rather
    # have orphan tokens (harmless: just sit in the table) than rewrite
    # to URLs that don't resolve.
    for r in rows_to_add:
        db.add(r)
    await db.commit()

    # Second pass: stitch the rewritten string from minted offsets in reverse
    # order so the absolute positions stay valid as we substitute.
    out = html
    for start, end, replacement in reversed(minted):
        out = out[:start] + replacement + out[end:]
    return out
