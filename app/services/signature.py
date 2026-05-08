"""
Renders the standardized BMP email signature for a User.

The signature template is fixed; only per-user fields vary.

scheduling_url falls back to the org-level iClosed booking URL when a
user hasn't set their own — so every BDR's signature has a "Schedule a
discovery call" link out of the box without per-user setup.
"""
from __future__ import annotations
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

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


def render_signature(user: User) -> str:
    template = _env.get_template("email_signature.html")
    return template.render(user={
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "nickname": user.nickname or "",
        "phone_number": user.phone_number or "",
        "email": user.email or "",
        "scheduling_url": effective_scheduling_url(user),
    })
