"""
Custom field definitions + value mutation endpoints.

Definitions: list / create / update / archive (soft-delete) — admin only.
Values:      simple PATCH on a Company or Contact swaps the field values
             via app.routes.company_routes / contact_routes (existing
             endpoints; this file only houses the meta-config).
"""
from __future__ import annotations
from typing import Optional, Any
import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db
from app.models import User, CustomFieldDefinition, Company, Contact
from app.auth import get_current_user, require_admin

router = APIRouter(prefix="/api/custom-fields", tags=["custom-fields"])


VALID_TYPES = {"text", "textarea", "number", "url", "email", "phone", "date", "select"}
SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


def _to_dict(d: CustomFieldDefinition) -> dict:
    options: list = []
    if d.options_json:
        try:
            options = json.loads(d.options_json) or []
        except Exception:
            options = []
    return {
        "id": d.id,
        "entity_type": d.entity_type,
        "key": d.key,
        "label": d.label,
        "field_type": d.field_type,
        "options": options,
        "helper_text": d.helper_text,
        "display_order": d.display_order,
        "is_active": d.is_active,
        "is_default": d.is_default,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


@router.get("")
async def list_definitions(
    entity_type: Optional[str] = None,
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List custom field definitions. Filter by entity_type
    ('company'|'contact') and toggle whether to include archived ones."""
    q = select(CustomFieldDefinition)
    if entity_type in ("company", "contact"):
        q = q.where(CustomFieldDefinition.entity_type == entity_type)
    if not include_inactive:
        q = q.where(CustomFieldDefinition.is_active == True)
    q = q.order_by(CustomFieldDefinition.display_order, CustomFieldDefinition.id)
    rows = (await db.execute(q)).scalars().all()
    return [_to_dict(r) for r in rows]


class CreateDefinitionRequest(BaseModel):
    entity_type: str
    key: str
    label: str
    field_type: str = "text"
    options: Optional[list[str]] = None
    helper_text: Optional[str] = None
    display_order: Optional[int] = 100


@router.post("")
async def create_definition(
    req: CreateDefinitionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    if req.entity_type not in ("company", "contact"):
        raise HTTPException(status_code=400, detail="entity_type must be 'company' or 'contact'")
    if req.field_type not in VALID_TYPES:
        raise HTTPException(status_code=400, detail=f"field_type must be one of: {sorted(VALID_TYPES)}")
    key = (req.key or "").strip().lower()
    if not SLUG_RE.match(key):
        raise HTTPException(status_code=400, detail="key must be lowercase letters/digits/underscores, start with a letter, max 80 chars")
    label = (req.label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="label is required")

    # Uniqueness check (entity_type, key)
    existing = (await db.execute(
        select(CustomFieldDefinition).where(
            CustomFieldDefinition.entity_type == req.entity_type,
            CustomFieldDefinition.key == key,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail=f"A field with key '{key}' already exists for {req.entity_type}")

    options_json = json.dumps(req.options) if (req.field_type == "select" and req.options) else None

    row = CustomFieldDefinition(
        entity_type=req.entity_type,
        key=key,
        label=label,
        field_type=req.field_type,
        options_json=options_json,
        helper_text=(req.helper_text or "").strip()[:200] or None,
        display_order=req.display_order if req.display_order is not None else 100,
        is_active=True,
        is_default=False,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _to_dict(row)


class UpdateDefinitionRequest(BaseModel):
    label: Optional[str] = None
    helper_text: Optional[str] = None
    display_order: Optional[int] = None
    options: Optional[list[str]] = None
    is_active: Optional[bool] = None


@router.patch("/{def_id}")
async def update_definition(
    def_id: int,
    req: UpdateDefinitionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    row = (await db.execute(
        select(CustomFieldDefinition).where(CustomFieldDefinition.id == def_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Definition not found")

    if req.label is not None:
        label = req.label.strip()
        if label:
            row.label = label
    if req.helper_text is not None:
        row.helper_text = req.helper_text.strip()[:200] or None
    if req.display_order is not None:
        row.display_order = int(req.display_order)
    if req.options is not None and row.field_type == "select":
        row.options_json = json.dumps(req.options) if req.options else None
    if req.is_active is not None:
        row.is_active = bool(req.is_active)

    await db.commit()
    await db.refresh(row)
    return _to_dict(row)


@router.delete("/{def_id}")
async def delete_definition(
    def_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Soft-delete: marks the definition is_active=False so existing
    field values on companies/contacts are preserved (they just won't
    show in forms). Hard delete is intentionally unavailable — preserves
    historical data on entities."""
    row = (await db.execute(
        select(CustomFieldDefinition).where(CustomFieldDefinition.id == def_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Definition not found")
    if row.is_default:
        raise HTTPException(status_code=400, detail="Default fields can be deactivated but not deleted. Use PATCH with is_active=false instead.")
    row.is_active = False
    await db.commit()
    return {"ok": True, "id": row.id, "is_active": row.is_active}


# ============================================================
# Value mutation — patch a Company or Contact's custom_fields_json
# ============================================================

class SetValuesRequest(BaseModel):
    values: dict  # {key: value} subset to merge into custom_fields_json


@router.patch("/values/company/{company_id}")
async def set_company_custom_fields(
    company_id: int,
    req: SetValuesRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Merge custom field values into a company's custom_fields_json.

    Only keys belonging to active company-level definitions are accepted —
    so a stale definition or a typo can't pollute the JSON. Empty-string
    values delete the key (so the user can clear a field cleanly)."""
    company = (await db.execute(
        select(Company).where(Company.id == company_id)
    )).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    from app.scoping import check_company_access
    if not check_company_access(company, user):
        raise HTTPException(status_code=404, detail="Company not found")

    valid_keys = set((await db.execute(
        select(CustomFieldDefinition.key).where(
            CustomFieldDefinition.entity_type == "company",
            CustomFieldDefinition.is_active == True,
        )
    )).scalars().all())

    current = {}
    if company.custom_fields_json:
        try: current = json.loads(company.custom_fields_json) or {}
        except Exception: current = {}

    for k, v in (req.values or {}).items():
        if k not in valid_keys:
            continue
        if v is None or (isinstance(v, str) and v.strip() == ""):
            current.pop(k, None)
        else:
            current[k] = v

    company.custom_fields_json = json.dumps(current) if current else None
    await db.commit()
    return {"ok": True, "custom_fields": current}


@router.patch("/values/contact/{contact_id}")
async def set_contact_custom_fields(
    contact_id: int,
    req: SetValuesRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Same as set_company_custom_fields but for Contact rows."""
    contact = (await db.execute(
        select(Contact).where(Contact.id == contact_id)
    )).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    from app.scoping import check_contact_access
    if not await check_contact_access(contact, user, db):
        raise HTTPException(status_code=404, detail="Contact not found")

    valid_keys = set((await db.execute(
        select(CustomFieldDefinition.key).where(
            CustomFieldDefinition.entity_type == "contact",
            CustomFieldDefinition.is_active == True,
        )
    )).scalars().all())

    current = {}
    if contact.custom_fields_json:
        try: current = json.loads(contact.custom_fields_json) or {}
        except Exception: current = {}

    for k, v in (req.values or {}).items():
        if k not in valid_keys:
            continue
        if v is None or (isinstance(v, str) and v.strip() == ""):
            current.pop(k, None)
        else:
            current[k] = v

    contact.custom_fields_json = json.dumps(current) if current else None
    await db.commit()
    return {"ok": True, "custom_fields": current}
