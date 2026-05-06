"""
Saved views / filter presets for Companies and Pipeline pages.
"""
from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from app.database import get_db
from app.models import User, SavedView
from app.auth import get_current_user
import json

router = APIRouter(prefix="/api/views", tags=["views"])


class CreateViewRequest(BaseModel):
    page: str  # "companies" or "pipeline"
    name: str
    filters: dict


@router.get("/")
async def list_views(
    page: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List saved views for the current user, optionally filtered by page."""
    query = select(SavedView).where(SavedView.user_id == user.id)
    if page:
        query = query.where(SavedView.page == page)
    query = query.order_by(SavedView.name)

    result = await db.execute(query)
    views = result.scalars().all()
    return [
        {
            "id": v.id,
            "page": v.page,
            "name": v.name,
            "filters": json.loads(v.filters_json),
            "created_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in views
    ]


@router.post("/")
async def create_view(
    req: CreateViewRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if req.page not in ("companies", "pipeline"):
        raise HTTPException(status_code=400, detail="Page must be 'companies' or 'pipeline'")

    view = SavedView(
        user_id=user.id,
        page=req.page,
        name=req.name,
        filters_json=json.dumps(req.filters),
    )
    db.add(view)
    await db.commit()
    await db.refresh(view)

    return {
        "id": view.id,
        "page": view.page,
        "name": view.name,
        "filters": req.filters,
    }


@router.delete("/{view_id}")
async def delete_view(
    view_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(SavedView).where(SavedView.id == view_id))
    view = result.scalar_one_or_none()
    if not view:
        raise HTTPException(status_code=404, detail="View not found")
    if view.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your view")

    await db.delete(view)
    await db.commit()
    return {"deleted": True}
