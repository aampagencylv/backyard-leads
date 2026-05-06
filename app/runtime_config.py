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


def mask_key(value: str | None) -> str:
    """Show only last 4 chars: 'pk_live_...c82a'"""
    if not value:
        return ""
    v = value.strip()
    if len(v) <= 8:
        return "*" * len(v)
    return f"{v[:8]}...{v[-4:]}"
