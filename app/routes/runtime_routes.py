"""
Org-level runtime config endpoints — Netrows + Twilio API keys.
Surfaced in the Settings UI so the team can rotate keys without SSH.
"""
from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.database import get_db
from app.models import User
from app.auth import get_current_user
from app.runtime_config import (
    _get_or_create,
    set_netrows_api_key,
    set_twilio_credentials,
    set_deepgram_api_key,
    set_blooio_api_key,
    set_blooio_signing_secret,
    set_resend_webhook_secret,
    set_messaging_direction,
    DEFAULT_MESSAGING_DIRECTION,
    mask_key,
)

router = APIRouter(prefix="/api", tags=["runtime-config"])


class UpdateRuntimeConfigRequest(BaseModel):
    netrows_api_key: Optional[str] = None
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_api_key_sid: Optional[str] = None
    twilio_api_key_secret: Optional[str] = None
    twilio_twiml_app_sid: Optional[str] = None
    deepgram_api_key: Optional[str] = None
    blooio_api_key: Optional[str] = None
    blooio_signing_secret: Optional[str] = None
    resend_webhook_secret: Optional[str] = None
    messaging_direction: Optional[str] = None


def _payload(rc, settings_obj) -> dict:
    netrows_db = (rc.netrows_api_key or "").strip()
    netrows_env = settings_obj.netrows_api_key or ""
    netrows_eff = netrows_db or netrows_env

    def t(field: str | None) -> dict:
        v = (field or "").strip()
        return {"set": bool(v), "masked": mask_key(v)}

    return {
        "netrows": {
            "set": bool(netrows_eff),
            "source": "database" if netrows_db else ("env" if netrows_env else "none"),
            "masked": mask_key(netrows_eff),
            "updated_at": rc.updated_at.isoformat() if rc.updated_at else None,
        },
        "twilio": {
            "account_sid":    t(rc.twilio_account_sid),
            "auth_token":     t(rc.twilio_auth_token),
            "api_key_sid":    t(rc.twilio_api_key_sid),
            "api_key_secret": t(rc.twilio_api_key_secret),
            "twiml_app_sid":  t(rc.twilio_twiml_app_sid),
            "minimally_configured": bool((rc.twilio_account_sid or "").strip() and (rc.twilio_auth_token or "").strip()),
            "voice_sdk_ready": all([(rc.twilio_account_sid or "").strip(),
                                    (rc.twilio_auth_token or "").strip(),
                                    (rc.twilio_api_key_sid or "").strip(),
                                    (rc.twilio_api_key_secret or "").strip(),
                                    (rc.twilio_twiml_app_sid or "").strip()]),
        },
        "deepgram": {
            "set": bool((rc.deepgram_api_key or "").strip()),
            "masked": mask_key(rc.deepgram_api_key),
        },
        "blooio": {
            "set": bool((rc.blooio_api_key or "").strip()),
            "masked": mask_key(rc.blooio_api_key),
            "signing_secret_set": bool((rc.blooio_signing_secret or "").strip()),
            "signing_secret_masked": mask_key(rc.blooio_signing_secret),
        },
        "resend": {
            # DB value preferred; .env value used as fallback. Show source so user
            # knows where to rotate ("database" means it's settable from this UI).
            "webhook_secret_db_set": bool((rc.resend_webhook_secret or "").strip()),
            "webhook_secret_env_set": bool((settings_obj.resend_webhook_secret or "").strip()),
            "webhook_secret_source":
                "database" if (rc.resend_webhook_secret or "").strip()
                else ("env" if (settings_obj.resend_webhook_secret or "").strip() else "none"),
            "webhook_secret_masked": mask_key((rc.resend_webhook_secret or "").strip() or settings_obj.resend_webhook_secret),
        },
        "messaging": {
            "direction": (rc.messaging_direction or "").strip(),
            "is_custom": bool((rc.messaging_direction or "").strip()),
            "default_preview": DEFAULT_MESSAGING_DIRECTION[:240] + "…",
        },
    }


@router.get("/runtime-config")
async def get_runtime_config(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.config import settings
    rc = await _get_or_create(db)
    return _payload(rc, settings)


@router.get("/runtime-config/messaging-default")
async def get_messaging_default(
    user: User = Depends(get_current_user),
):
    """Returns the full in-code default messaging direction text — used by
    the Settings UI's 'Load Default' button so the user can see / edit it
    before saving as a custom direction."""
    return {"text": DEFAULT_MESSAGING_DIRECTION}


@router.patch("/runtime-config")
async def update_runtime_config(
    req: UpdateRuntimeConfigRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Update API keys. Super admin only."""
    if user.role != "super_admin":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Only super admins can modify API keys")
    if req.netrows_api_key is not None:
        await set_netrows_api_key(db, req.netrows_api_key)

    twilio_changes = {
        "account_sid":    req.twilio_account_sid,
        "auth_token":     req.twilio_auth_token,
        "api_key_sid":    req.twilio_api_key_sid,
        "api_key_secret": req.twilio_api_key_secret,
        "twiml_app_sid":  req.twilio_twiml_app_sid,
    }
    if any(v is not None for v in twilio_changes.values()):
        await set_twilio_credentials(db, **twilio_changes)

    if req.deepgram_api_key is not None:
        await set_deepgram_api_key(db, req.deepgram_api_key)
    if req.blooio_api_key is not None:
        await set_blooio_api_key(db, req.blooio_api_key)
    if req.blooio_signing_secret is not None:
        await set_blooio_signing_secret(db, req.blooio_signing_secret)
    if req.resend_webhook_secret is not None:
        await set_resend_webhook_secret(db, req.resend_webhook_secret)
    if req.messaging_direction is not None:
        await set_messaging_direction(db, req.messaging_direction)

    from app.config import settings
    rc = await _get_or_create(db)
    return _payload(rc, settings)
