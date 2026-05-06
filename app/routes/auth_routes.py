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


class InviteUserRequest(BaseModel):
    email: str
    first_name: str
    last_name: str
    role: str = "sales_rep"
    title: Optional[str] = None


@router.post("/users/invite")
async def invite_user(
    req: InviteUserRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """
    Admin creates a new user account with a temporary password.
    The user can change their password after first login.
    """
    allowed_domains = ["backyardmarketingpros.com", "aamp.agency"]
    email_domain = req.email.strip().lower().split("@")[-1]
    if email_domain not in allowed_domains:
        raise HTTPException(status_code=400, detail="Email must be @backyardmarketingpros.com or @aamp.agency")

    existing = await db.execute(select(User).where(User.email == req.email.lower()))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    if req.role not in ("admin", "sales_rep", "read_only"):
        raise HTTPException(status_code=400, detail="Invalid role")

    # Generate a temporary password
    import secrets
    temp_password = secrets.token_urlsafe(12)

    new_user = User(
        email=req.email.lower(),
        first_name=req.first_name.strip(),
        last_name=req.last_name.strip(),
        nickname=req.title or "",
        hashed_password=hash_password(temp_password),
        role=req.role,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    return {
        "id": new_user.id,
        "email": new_user.email,
        "name": new_user.full_name,
        "role": new_user.role,
        "temp_password": temp_password,
        "message": f"User created. Temporary password: {temp_password} — share this securely with the user.",
    }


class UpdateUserRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    nickname: Optional[str] = None  # title/nickname
    role: Optional[str] = None
    sending_enabled: Optional[bool] = None
    is_active: Optional[bool] = None


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int,
    req: UpdateUserRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Update any user's profile (admin only)."""
    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if req.first_name is not None:
        target.first_name = req.first_name
    if req.last_name is not None:
        target.last_name = req.last_name
    if req.nickname is not None:
        target.nickname = req.nickname
    if req.role is not None and req.role in ("admin", "sales_rep", "read_only"):
        target.role = req.role
    if req.sending_enabled is not None:
        target.sending_enabled = req.sending_enabled
    if req.is_active is not None:
        target.is_active = req.is_active

    await db.commit()
    return {
        "id": target.id,
        "email": target.email,
        "name": target.full_name,
        "role": target.role,
        "nickname": target.nickname,
        "sending_enabled": target.sending_enabled,
        "is_active": target.is_active,
    }
