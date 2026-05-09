"""
Upload routes.

`POST /api/uploads/logo` is the generic image-upload endpoint used by
both the Calendar booking-page branding and (next) the audit-report
header customization. Accepts multipart/form-data with one `file`
part. Returns `{url: "https://..."}` — caller persists the URL on
whatever record needs it (SchedulingConfig.logo_url, etc.).
"""
from __future__ import annotations
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.auth import get_current_user
from app.models import User
from app.services.uploads import (
    ALLOWED_IMAGE_TYPES, MAX_LOGO_BYTES, UploadValidationError, save_image,
)

log = logging.getLogger("bmp.uploads")

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


@router.post("/logo")
async def upload_logo(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """Upload a logo image. Returns the absolute URL where the file
    is now reachable. The caller (Calendar Settings, audit settings,
    etc.) saves that URL on whichever record needs it."""
    if file.content_type and file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Image type '{file.content_type}' not allowed. "
                   "Use PNG, JPG, WebP, GIF, or SVG.",
        )
    content = await file.read()
    if len(content) > MAX_LOGO_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large ({len(content) // 1024}KB). "
                   f"Max is {MAX_LOGO_BYTES // 1024 // 1024}MB.",
        )
    try:
        url = save_image(
            content,
            content_type=file.content_type or "application/octet-stream",
            category="logos",
            user_id=user.id,
        )
    except UploadValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        log.exception(f"Logo upload failed for user {user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not save the upload. Try again or paste a URL instead.",
        )
    return {"url": url, "size_bytes": len(content), "content_type": file.content_type}
