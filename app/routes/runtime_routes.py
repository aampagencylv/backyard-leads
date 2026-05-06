"""
Org-level runtime config endpoints (currently just the Netrows API key).
Surfaced in the Settings UI so the team can rotate keys without SSH.
"""
from __future__ import annotations
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.database import get_db
from app.models import User
from app.auth import get_current_user
from app.runtime_config import _get_or_create, set_netrows_api_key, mask_key
from app.config import settings

router = APIRouter(prefix="/api", tags=["runtime-config"])


class UpdateRuntimeConfigRequest(BaseModel):
    netrows_api_key: str | None = None


@router.get("/runtime-config")
async def get_runtime_config(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return masked view of runtime keys + indication of whether each is set."""
    rc = await _get_or_create(db)
    db_value = (rc.netrows_api_key or "").strip()
    env_value = settings.netrows_api_key or ""
    effective = db_value or env_value
    return {
        "netrows": {
            "set": bool(effective),
            "source": "database" if db_value else ("env" if env_value else "none"),
            "masked": mask_key(effective),
            "updated_at": rc.updated_at.isoformat() if rc.updated_at else None,
        },
    }


@router.patch("/runtime-config")
async def update_runtime_config(
    req: UpdateRuntimeConfigRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Update API keys. Pass empty string to clear (falls back to env)."""
    if req.netrows_api_key is not None:
        await set_netrows_api_key(db, req.netrows_api_key)
    rc = await _get_or_create(db)
    db_value = (rc.netrows_api_key or "").strip()
    env_value = settings.netrows_api_key or ""
    effective = db_value or env_value
    return {
        "netrows": {
            "set": bool(effective),
            "source": "database" if db_value else ("env" if env_value else "none"),
            "masked": mask_key(effective),
            "updated_at": rc.updated_at.isoformat() if rc.updated_at else None,
        },
    }
