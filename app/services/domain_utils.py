"""
Canonical domain extraction for Company dedupe.

Normalizes any URL/website string to a single canonical lowercase domain so
two records with the same site can't end up as separate Company rows. This
is the bug Steve hit (2026-05-07) where AAMP Agency existed as two rows
both with website=https://aamp.agency — one from manual create, one from
Find Leads.

Examples:
  https://www.AAMP.agency/contact/  → 'aamp.agency'
  HTTP://aamp.agency                → 'aamp.agency'
  aamp.agency                       → 'aamp.agency'
  www.aamp.agency                   → 'aamp.agency'
  https://blog.aamp.agency/         → 'blog.aamp.agency'  (subdomains are kept on purpose)
  '' / None / 'localhost'            → None (not normalizable)
"""
from __future__ import annotations
from typing import Optional
from urllib.parse import urlparse


def normalize_domain(url_or_domain: Optional[str]) -> Optional[str]:
    """Return the canonical lowercase host for a URL/domain string, or None
    if the input isn't a real-looking domain.

    Subdomains are preserved (blog.example.com stays distinct from example.com)
    because they often represent intentionally different sites. The leading
    'www.' is the one canonicalization we always strip — it's universally
    treated as "the same site" by every browser + DNS resolver.
    """
    if not url_or_domain:
        return None
    s = url_or_domain.strip().lower()
    if not s:
        return None

    # Add a scheme if missing so urlparse can locate the netloc
    if "://" not in s:
        s = "http://" + s

    try:
        parsed = urlparse(s)
    except Exception:
        return None

    host = (parsed.netloc or "").split("@")[-1]  # strip user:pass@ if present
    host = host.split(":")[0]                     # strip :port
    if not host:
        return None

    # Strip leading www.
    if host.startswith("www."):
        host = host[4:]

    # Reject obviously non-domain inputs
    if host in ("localhost", ""):
        return None
    if "." not in host:
        return None
    # Reject pure IPs — we don't want to merge two unrelated apps that happen to be on the same server
    if all(part.isdigit() for part in host.split(".")):
        return None

    return host
