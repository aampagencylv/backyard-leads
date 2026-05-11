"""
Renders the standardized email signature for a User.

Org brand (logo, accent colors, company name, website URL) is pulled
live from RuntimeConfig via `get_org_brand`, so every signature shows
whatever the admin set in Settings → Org Brand. Per-user fields
(name, phone, email, scheduling URL) come from the User row.

scheduling_url falls back to the org-level iClosed booking URL when a
user hasn't set their own — so every BDR's signature has a 'Schedule a
discovery call' link out of the box without per-user setup.
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.config import settings

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


def effective_scheduling_url(user: User) -> str:
    """Per-user URL if set, else the BMP org iClosed booking URL.
    Returns empty string only when both are missing."""
    return (user.scheduling_url or "").strip() or (settings.iclosed_booking_url or "").strip()


# Hardcoded BMP defaults, used as a last-resort fallback when the brand
# resolver fails or when render_signature is called from a sync context
# that can't await a DB query (legacy paths only — all current callers
# should go through `render_signature(db, user)`).
_DEFAULT_BRAND = {
    "primary_color":   "#E65100",
    "secondary_color": "#1B5E20",
    "accent_bg_color": "#FFF8F0",
    "logo_url":        "https://backyardmarketingpros.com/wp-content/uploads/2024/08/BMP_Logo_Color_Horiz.png",
    "company_name":    "Backyard Marketing Pros",
    "website_url":     "https://backyardmarketingpros.com",
}


def _strip_scheme(url: str) -> str:
    """Render the website URL as 'www.example.com' rather than the full
    https:// for display purposes — matches the legacy signature's
    treatment of www.backyardmarketingpros.com."""
    s = (url or "").strip()
    if s.lower().startswith("https://"):
        s = s[8:]
    elif s.lower().startswith("http://"):
        s = s[7:]
    return s.rstrip("/")


async def render_signature(db: AsyncSession, user: User) -> str:
    """Async-aware signature renderer. Pulls org brand from RuntimeConfig
    so colors / logo / company name / website all reflect whatever the
    admin configured."""
    brand = dict(_DEFAULT_BRAND)
    try:
        from app.runtime_config import get_org_brand
        resolved = await get_org_brand(db)
        for k, v in resolved.items():
            if v:
                brand[k] = v
    except Exception:
        # Bootstrap path / migration mid-flight — fall back to BMP defaults
        # so the email still goes out instead of failing.
        pass

    template = _env.get_template("email_signature.html")
    return template.render(
        user={
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "nickname": user.nickname or "",
            "phone_number": user.phone_number or "",
            "email": user.email or "",
            "scheduling_url": effective_scheduling_url(user),
        },
        brand={
            "primary_color":   brand["primary_color"],
            "secondary_color": brand["secondary_color"],
            "accent_bg_color": brand["accent_bg_color"],
            "logo_url":        brand["logo_url"] or _DEFAULT_BRAND["logo_url"],
            "company_name":    brand["company_name"],
            "website_url":     brand["website_url"],
            "website_display": _strip_scheme(brand["website_url"]),
        },
    )
