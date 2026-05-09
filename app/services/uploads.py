"""
File upload service.

Designed to be reusable across multiple upload use cases — booking-
page logos today, audit-report header images soon, future tenant
branding. Each use case calls into `save_image()` with a `category`
that becomes a sub-path under `var/uploads/`.

Storage layout:
  var/uploads/{category}/{user_id}/{random}.{ext}

Public URL:
  {public_url}/uploads/{category}/{user_id}/{random}.{ext}

Constraints:
  - Image-only (PNG / JPG / WebP / GIF / SVG)
  - 2 MB hard cap
  - Random filename — never trust user-supplied filenames
  - Per-user subdirectory so multi-tenant isolation is naturally
    enforced when SaaS lands
"""
from __future__ import annotations
import secrets
from pathlib import Path
from typing import Optional

from app.config import settings


# Filesystem path (mounted at /uploads/ in main.py via StaticFiles)
UPLOAD_BASE = Path("var/uploads")

# Mapping content-type → extension. We use the content-type, NOT the
# user-supplied filename's extension, so a malicious .php file
# masquerading as image/png lands as .png on disk.
ALLOWED_IMAGE_TYPES: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/svg+xml": "svg",
}

MAX_LOGO_BYTES = 2 * 1024 * 1024  # 2 MB


def ensure_upload_dirs() -> None:
    """Called from main.py at startup so the StaticFiles mount has a
    real directory to attach to (otherwise Starlette raises at boot)."""
    UPLOAD_BASE.mkdir(parents=True, exist_ok=True)


class UploadValidationError(ValueError):
    """Raised for caller-fixable problems — bad type, too large, etc.
    Routes catch this and surface as HTTP 400."""


def _safe_ext(content_type: Optional[str]) -> str:
    if not content_type:
        raise UploadValidationError("Missing Content-Type header")
    ct = content_type.lower().split(";", 1)[0].strip()
    if ct not in ALLOWED_IMAGE_TYPES:
        raise UploadValidationError(
            f"Unsupported image type: {ct}. Use PNG, JPG, WebP, GIF, or SVG."
        )
    return ALLOWED_IMAGE_TYPES[ct]


def save_image(
    content: bytes, content_type: str, *, category: str, user_id: int,
) -> str:
    """Persist `content` under var/uploads/{category}/{user_id}/{rand}.{ext}.
    Returns the absolute https URL the booking page (or any other
    consumer) can use directly. Never overwrites — random filename
    means uploads are cumulative; old logos stay on disk after a
    replace, which is fine at our scale."""
    if not category or not category.replace("_", "").isalnum():
        # Defensive: don't let a future caller smuggle path separators
        # in via a category param.
        raise UploadValidationError("Invalid upload category")
    ext = _safe_ext(content_type)
    if len(content) > MAX_LOGO_BYTES:
        raise UploadValidationError(
            f"File too large ({len(content) // 1024}KB). Max is {MAX_LOGO_BYTES // 1024 // 1024}MB."
        )

    user_dir = UPLOAD_BASE / category / str(int(user_id))
    user_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_urlsafe(12)}.{ext}"
    fpath = user_dir / filename
    fpath.write_bytes(content)

    # Always return an absolute URL — the booking-page logo_url
    # validator requires https://, and absolute URLs are easier to
    # paste into other tools later.
    public = settings.public_url.rstrip("/")
    return f"{public}/uploads/{category}/{int(user_id)}/{filename}"
