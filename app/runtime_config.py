"""
Runtime configuration helpers.

Per-row config (currently single-row) lives in the runtime_config table and
overrides env-var defaults. This lets the team rotate API keys (e.g. Netrows)
from the Settings UI without SSHing into the server.

Read path is: DB row → env-var fallback. Write path goes through the
Settings UI (PATCH /api/runtime-config).
"""
from __future__ import annotations
from typing import Optional
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import RuntimeConfig
from app.config import settings
from app.services.twilio_voice import TwilioCredentials


async def _get_or_create(db: AsyncSession) -> RuntimeConfig:
    rc = (await db.execute(select(RuntimeConfig).where(RuntimeConfig.id == 1))).scalar_one_or_none()
    if rc is None:
        rc = RuntimeConfig(id=1)
        db.add(rc)
        await db.commit()
        await db.refresh(rc)
    return rc


async def get_netrows_api_key(db: AsyncSession) -> str:
    rc = await _get_or_create(db)
    return (rc.netrows_api_key or "").strip() or settings.netrows_api_key or ""


async def set_netrows_api_key(db: AsyncSession, value: str) -> RuntimeConfig:
    rc = await _get_or_create(db)
    rc.netrows_api_key = value.strip() or None
    rc.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(rc)
    return rc


async def get_twilio_credentials(db: AsyncSession) -> TwilioCredentials:
    """Pull Twilio creds from runtime_config; field-level env fallback."""
    rc = await _get_or_create(db)
    return TwilioCredentials(
        account_sid=(rc.twilio_account_sid or "").strip() or "",
        auth_token=(rc.twilio_auth_token or "").strip() or "",
        api_key_sid=(rc.twilio_api_key_sid or "").strip() or None,
        api_key_secret=(rc.twilio_api_key_secret or "").strip() or None,
        twiml_app_sid=(rc.twilio_twiml_app_sid or "").strip() or None,
    )


async def set_twilio_credentials(
    db: AsyncSession,
    *,
    account_sid: str | None = None,
    auth_token: str | None = None,
    api_key_sid: str | None = None,
    api_key_secret: str | None = None,
    twiml_app_sid: str | None = None,
) -> RuntimeConfig:
    rc = await _get_or_create(db)
    if account_sid is not None:
        rc.twilio_account_sid = account_sid.strip() or None
    if auth_token is not None:
        rc.twilio_auth_token = auth_token.strip() or None
    if api_key_sid is not None:
        rc.twilio_api_key_sid = api_key_sid.strip() or None
    if api_key_secret is not None:
        rc.twilio_api_key_secret = api_key_secret.strip() or None
    if twiml_app_sid is not None:
        rc.twilio_twiml_app_sid = twiml_app_sid.strip() or None
    rc.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(rc)
    return rc


def mask_key(value: str | None) -> str:
    """Show only last 4 chars: 'pk_live_...c82a'"""
    if not value:
        return ""
    v = value.strip()
    if len(v) <= 8:
        return "*" * len(v)
    return f"{v[:8]}...{v[-4:]}"
