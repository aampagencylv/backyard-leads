"""
Renders the standardized BMP email signature for a User.

The signature template is fixed; only per-user fields vary.
"""
from __future__ import annotations
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.models import User

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


def render_signature(user: User) -> str:
    template = _env.get_template("email_signature.html")
    return template.render(user={
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "nickname": user.nickname or "",
        "phone_number": user.phone_number or "",
        "email": user.email or "",
        "scheduling_url": user.scheduling_url or "",
    })
