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
) -> str:
    """Replace every trackable href in `html` with a /t/{token} URL.
    Mints one TrackingLink row per URL. Commits in batches at the end —
    caller can wrap in their own transaction if needed; this commits to
    keep tokens persistent in case the email send itself fails afterwards
    (better to have orphan tokens than to drop tracking on a partial send).

    Returns the rewritten HTML. If no trackable links are found, returns
    the input unchanged (no DB writes)."""
    if not html:
        return html

    # First pass: scan for all hrefs we'll track. Build them up in order so we
    # can mint tokens and substitute back in one pass.
    matches = list(_HREF_RE.finditer(html))
    if not matches:
        return html

    public_base = settings.public_url.rstrip("/")
    minted: list[tuple[int, int, str]] = []  # (start, end, replacement)
    rows_to_add: list[TrackingLink] = []

    for m in matches:
        full_match = m.group(0)
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

    if not minted:
        return html

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
