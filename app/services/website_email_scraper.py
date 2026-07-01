"""
Website Email Scraper — last-resort contact discovery.

Netrows + Hunter cover US-corporate prospects well but miss small/owner-run
businesses (Caribbean tour operators, single-location SMBs) that never appear
in B2B contact databases. Those businesses almost always publish a real inbox
(info@, contact@, the owner's personal yahoo/gmail) right on their own site.

This module fetches the homepage + a few common contact pages, extracts every
email it can find (raw text + mailto: links), filters out the noise that makes
naive scraping useless (Wix/Sentry telemetry addresses, placeholder samples,
image filenames), and ranks what's left so the BEST single inbox surfaces
first: same-domain role inboxes > same-domain anything > free-mail found on the
page. It is a fallback only — called when the paid providers return nothing.
"""
from __future__ import annotations
import re
import asyncio
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

# Pages most likely to carry a contact address, in priority order. We fetch a
# small fixed set rather than crawling — cheap, bounded, and enough in practice.
_CANDIDATE_PATHS = ["", "/contact", "/contact-us", "/contacts", "/about", "/about-us", "/book", "/booking"]

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Free-mail providers — small operators legitimately use these as their real
# business inbox (e.g. captdansea@yahoo.com), so we keep them, but rank them
# below an on-domain address.
_FREEMAIL = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "me.com", "live.com", "msn.com", "ymail.com", "comcast.net",
}

# Role inboxes worth preferring — a human reads these.
_ROLE_LOCALPARTS = {
    "info", "contact", "hello", "bookings", "booking", "reservations",
    "reserve", "sales", "office", "admin", "charters", "charter", "tours",
    "sail", "crew", "captain", "ahoy", "mail", "inquiries", "enquiries",
}

# Domains/substrings that are never a real prospect inbox — telemetry,
# analytics, CDNs, page builders, and our own infrastructure.
_NOISE_DOMAIN_SUBSTR = (
    "sentry", "wixpress.com", "wix.com", "sentry.io", "godaddy",
    "squarespace", "shopify", "cloudflare", "googleusercontent", "gstatic",
    "schema.org", "w3.org", "example.com", "example.org", "domain.com",
    "yourdomain", "email.com", "test.com", "sentry-next",
    "mysite.com", "wixsite.com", "godaddysites.com", "weebly.com",
)

# Placeholder localparts that ship inside site templates.
_NOISE_LOCALPARTS = {"example", "sample", "yourname", "youremail", "email", "test"}

# Exact placeholder addresses that ship inside templates.
_NOISE_EXACT = {
    "user@domain.com", "your@email.com", "name@email.com", "email@example.com",
    "name@example.com", "you@example.com", "john@doe.com", "first.last@example.com",
    "someone@example.com", "username@gmail.com", "youremail@gmail.com",
}

# Localparts that signal an asset/library reference rather than a mailbox.
_NOISE_LOCALPART_SUBSTR = ("@2x", "@3x", "sentry", "react", "webpack", "sprite")

_ASSET_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js", ".ico")


@dataclass
class ScrapedEmail:
    email: str
    name: str          # "" for role/unknown inboxes — generator falls back to a plain greeting
    source_url: str     # which page it came from
    score: int          # higher = better; used for ranking


def _root_domain(host: str) -> str:
    """Reduce a hostname to its registrable-ish root (drops www and obvious
    subdomains). Good enough to compare an email domain against the site."""
    host = (host or "").lower().lstrip("www.")
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _is_noise(email: str) -> bool:
    e = email.lower()
    if e in _NOISE_EXACT:
        return True
    if any(s in e for s in _NOISE_LOCALPART_SUBSTR):
        return True
    if e.endswith(_ASSET_EXT):
        return True
    domain = e.split("@", 1)[1] if "@" in e else ""
    if any(s in domain for s in _NOISE_DOMAIN_SUBSTR):
        return True
    if e.split("@", 1)[0] in _NOISE_LOCALPARTS:
        return True
    # Hex-blob localparts (Wix/Sentry event ids look like emails) — 24+ hex chars.
    local = e.split("@", 1)[0]
    if len(local) >= 24 and re.fullmatch(r"[0-9a-f]+", local):
        return True
    return False


def _name_from_localpart(local: str) -> str:
    """Best-effort person name from a localpart. Returns "" for role inboxes
    and anything that doesn't look like firstname or first.last — we'd rather
    send a nameless 'Hi' than guess wrong and address a stranger by a handle."""
    base = local.lower()
    if base in _ROLE_LOCALPARTS:
        return ""
    if "." in base:
        a, b = base.split(".", 1)
        if a.isalpha() and b.isalpha() and 1 < len(a) < 20 and 1 < len(b) < 20:
            return f"{a.capitalize()} {b.capitalize()}"
    return ""


def _score(email: str, site_root: str) -> int:
    local, domain = email.lower().split("@", 1)
    domain_root = _root_domain(domain)
    on_domain = site_root and domain_root == site_root
    role = local in _ROLE_LOCALPARTS
    freemail = domain in _FREEMAIL
    if on_domain and role:
        return 100
    if on_domain:
        return 80
    if role and not freemail:
        return 60          # role inbox on a slightly different domain
    if freemail:
        return 40          # owner's personal inbox listed on the page
    return 20


async def scrape_site_emails(
    website: str, *, max_results: int = 3, timeout: float = 10.0, min_score: int = 40,
) -> list[ScrapedEmail]:
    """Fetch a few pages of `website` and return ranked, de-noised emails.

    Never raises — any network/parse failure yields fewer (or zero) results.
    Bounded: at most len(_CANDIDATE_PATHS) small GETs, short timeout.

    min_score (default 40) drops low-confidence hits: an email that is neither
    on the site's own domain, nor a role inbox, nor a free-mail provider scores
    20 and is REJECTED — those are usually a vendor/agency/tracking address that
    happens to appear on the page (e.g. our own t@aamp.agency leaking onto a
    prospect's site), never the prospect's real inbox. Better no contact than a
    wrong one that emails the wrong party."""
    if not website:
        return []
    url = website if website.startswith("http") else f"https://{website}"
    parsed = urlparse(url)
    site_root = _root_domain(parsed.netloc)
    base = f"{parsed.scheme}://{parsed.netloc}"

    found: dict[str, ScrapedEmail] = {}

    async def _grab(client: httpx.AsyncClient, path: str) -> None:
        try:
            r = await client.get(base + path)
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", "text/html"):
                # Still try raw regex on non-HTML in case of text/plain contact pages.
                pass
            html = r.text or ""
        except Exception:
            return
        page_url = base + path
        candidates: set[str] = set(_EMAIL_RE.findall(html))
        # mailto: links are the highest-signal source — parse them explicitly.
        try:
            soup = BeautifulSoup(html, "lxml")
            for a in soup.select('a[href^="mailto:"]'):
                href = a.get("href", "")
                addr = href[len("mailto:"):].split("?", 1)[0].strip()
                if addr:
                    candidates.add(addr)
        except Exception:
            pass
        for raw in candidates:
            email = raw.strip().strip(".").lower()
            if "@" not in email or _is_noise(email):
                continue
            if email in found:
                continue
            local = email.split("@", 1)[0]
            found[email] = ScrapedEmail(
                email=email,
                name=_name_from_localpart(local),
                source_url=page_url,
                score=_score(email, site_root),
            )

    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; BackyardLeads/1.0)"},
        ) as client:
            # Homepage first (cheap, usually has the contact link); then the
            # rest concurrently but bounded.
            await _grab(client, _CANDIDATE_PATHS[0])
            await asyncio.gather(*(_grab(client, p) for p in _CANDIDATE_PATHS[1:]))
    except Exception:
        pass

    ranked = [e for e in found.values() if e.score >= min_score]
    return sorted(ranked, key=lambda e: e.score, reverse=True)[:max_results]
