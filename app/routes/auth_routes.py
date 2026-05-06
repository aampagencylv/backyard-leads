from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel
from app.database import get_db
from app.models import User
from app.auth import hash_password, verify_password, create_access_token, get_current_user, require_admin

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: str
    first_name: str
    last_name: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_name: str
    user_email: str


@router.post("/register", response_model=TokenResponse)
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    # Restrict registration to company emails
    allowed_domains = ["backyardmarketingpros.com", "aamp.agency"]
    email_domain = req.email.strip().lower().split("@")[-1]
    if email_domain not in allowed_domains:
        raise HTTPException(status_code=403, detail="Registration is restricted to Backyard Marketing Pros team members.")

    # Check if email already exists
    result = await db.execute(select(User).where(User.email == req.email.lower()))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    # First user gets admin role
    count_result = await db.execute(select(func.count()).select_from(User))
    user_count = count_result.scalar()
    role = "admin" if user_count == 0 else "sales_rep"

    user = User(
        email=req.email.lower(),
        first_name=req.first_name.strip(),
        last_name=req.last_name.strip(),
        hashed_password=hash_password(req.password),
        role=role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token({"sub": str(user.id)})
    return TokenResponse(
        access_token=token,
        user_name=user.full_name,
        user_email=user.email,
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == form_data.username.lower()))
    user = result.scalar_one_or_none()

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token({"sub": str(user.id)})
    return TokenResponse(
        access_token=token,
        user_name=user.full_name,
        user_email=user.email,
    )


@router.get("/me")
async def get_me(user: User = Depends(get_current_user)):
    return {
        "id": user.id,
        "email": user.email,
        "name": user.full_name,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "nickname": user.nickname,
        "phone_number": user.phone_number,
        "scheduling_url": user.scheduling_url,
        "role": user.role,
        "sending_enabled": user.sending_enabled,
    }


# ============ Admin: User Management ============

@router.get("/users")
async def list_users(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """List all users (admin only)."""
    result = await db.execute(select(User).order_by(User.created_at))
    users = result.scalars().all()
    return [
        {
            "id": u.id,
            "email": u.email,
            "name": u.full_name,
            "role": u.role,
            "sending_enabled": u.sending_enabled,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


class UpdateUserRoleRequest(BaseModel):
    role: str  # admin, sales_rep, read_only


@router.patch("/users/{user_id}/role")
async def update_user_role(
    user_id: int,
    req: UpdateUserRoleRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Change a user's role (admin only)."""
    if req.role not in ("admin", "sales_rep", "read_only"):
        raise HTTPException(status_code=400, detail="Invalid role")

    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    target.role = req.role
    await db.commit()
    return {"id": target.id, "name": target.full_name, "role": target.role}
