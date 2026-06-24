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

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status

from app.auth import get_current_user
from app.models import User
from app.services.uploads import (
    ALLOWED_IMAGE_TYPES, ALLOWED_AUDIO_TYPES, MAX_LOGO_BYTES, MAX_AUDIO_BYTES,
    UploadValidationError, save_image, save_audio,
)

log = logging.getLogger("bmp.uploads")

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


@router.post("/logo")
async def upload_logo(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """Upload a logo image. Returns the absolute URL where the file
    is now reachable. The caller (Calendar Settings, audit settings,
    etc.) saves that URL on whichever record needs it. The URL is built
    on the requesting tenant's own host so a white-label tenant's logo
    never points at another tenant's domain."""
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
    # Build the asset URL on the tenant's own host (scheme + host from the
    # request), falling back to settings.public_url inside save_image when
    # the header is missing.
    tenant_base = None
    host = request.headers.get("host")
    if host:
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
        tenant_base = f"{scheme}://{host}"
    try:
        url = save_image(
            content,
            content_type=file.content_type or "application/octet-stream",
            category="logos",
            user_id=user.id,
            base_url=tenant_base,
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


@router.post("/voicemail-greeting")
async def upload_voicemail_greeting(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """Upload a custom voicemail greeting audio file. Saves the relative
    URL on the User model so inbound calls play it instead of TTS."""
    if file.content_type and file.content_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Audio type '{file.content_type}' not allowed. "
                   "Use MP3, WAV, OGG, or WebM.",
        )
    content = await file.read()
    if len(content) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large ({len(content) // 1024}KB). Max is {MAX_AUDIO_BYTES // 1024 // 1024}MB.",
        )
    try:
        relative_url = save_audio(
            content,
            content_type=file.content_type or "audio/mpeg",
            category="voicemail",
            user_id=user.id,
        )
    except UploadValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        log.exception(f"Voicemail greeting upload failed for user {user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not save the upload. Try again.",
        )

    # Save on user profile
    from app.database import get_db
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy import select
    from app.database import async_session
    async with async_session() as db:
        u = (await db.execute(select(User).where(User.id == user.id))).scalar_one_or_none()
        if u:
            u.voicemail_greeting_url = relative_url
            await db.commit()

    return {"url": relative_url, "size_bytes": len(content), "content_type": file.content_type}


@router.delete("/voicemail-greeting")
async def delete_voicemail_greeting(
    user: User = Depends(get_current_user),
):
    """Remove custom voicemail greeting — reverts to TTS."""
    from app.database import async_session
    from sqlalchemy import select
    async with async_session() as db:
        u = (await db.execute(select(User).where(User.id == user.id))).scalar_one_or_none()
        if u:
            u.voicemail_greeting_url = None
            await db.commit()
    return {"deleted": True}
