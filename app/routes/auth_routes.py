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

    # Send welcome email with credentials via Resend
    email_sent = False
    try:
        from app.config import settings
        if settings.resend_api_key:
            import httpx
            await httpx.AsyncClient(timeout=10).post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key}", "Content-Type": "application/json"},
                json={
                    "from": f"Backyard Marketing Pros <noreply@{settings.send_domain}>",
                    "to": [new_user.email],
                    "subject": "Welcome to BMP Prospector — Your Account is Ready",
                    "html": f"""
                    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:500px;margin:0 auto;padding:20px">
                        <img src="https://backyardmarketingpros.com/wp-content/uploads/2024/08/BMP_Logo_Color_Horiz-1024x269.png" style="width:250px;margin-bottom:20px" alt="BMP">
                        <h2 style="color:#1B5E20">Welcome to Prospector, {new_user.first_name}!</h2>
                        <p>Your account has been created. Here are your login credentials:</p>
                        <div style="background:#f5f7f5;border-radius:8px;padding:16px;margin:16px 0">
                            <p><strong>URL:</strong> <a href="{settings.public_url}">{settings.public_url}</a></p>
                            <p><strong>Email:</strong> {new_user.email}</p>
                            <p><strong>Temporary Password:</strong> {temp_password}</p>
                        </div>
                        <p style="color:#666;font-size:13px">Please change your password after your first login by going to Settings.</p>
                        <p>— The BMP Team</p>
                    </div>
                    """,
                },
            )
            email_sent = True
    except Exception:
        pass

    return {
        "id": new_user.id,
        "email": new_user.email,
        "name": new_user.full_name,
        "role": new_user.role,
        "temp_password": temp_password,
        "welcome_email_sent": email_sent,
        "message": f"User created. {'Welcome email sent!' if email_sent else 'Temporary password: ' + temp_password}",
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


# ============ Password Management ============

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Change your own password (requires current password)."""
    if not verify_password(req.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")

    user.hashed_password = hash_password(req.new_password)
    await db.commit()
    return {"message": "Password changed successfully"}


class ForgotPasswordRequest(BaseModel):
    email: str


@router.post("/forgot-password")
async def forgot_password(
    req: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """Send a password reset email with a temporary new password."""
    result = await db.execute(select(User).where(User.email == req.email.strip().lower()))
    user = result.scalar_one_or_none()

    # Always return success to prevent email enumeration
    if not user:
        return {"message": "If that email exists, a reset link has been sent."}

    import secrets
    temp_password = secrets.token_urlsafe(10)
    user.hashed_password = hash_password(temp_password)
    await db.commit()

    # Send reset email
    try:
        from app.config import settings
        if settings.resend_api_key:
            import httpx
            await httpx.AsyncClient(timeout=10).post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key}", "Content-Type": "application/json"},
                json={
                    "from": f"Backyard Marketing Pros <noreply@{settings.send_domain}>",
                    "to": [user.email],
                    "subject": "Password Reset — BMP Prospector",
                    "html": f"""
                    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:500px;margin:0 auto;padding:20px">
                        <img src="https://backyardmarketingpros.com/wp-content/uploads/2024/08/BMP_Logo_Color_Horiz-1024x269.png" style="width:250px;margin-bottom:20px" alt="BMP">
                        <h2 style="color:#1B5E20">Password Reset</h2>
                        <p>Hi {user.first_name}, your password has been reset.</p>
                        <div style="background:#f5f7f5;border-radius:8px;padding:16px;margin:16px 0">
                            <p><strong>Your new temporary password:</strong> {temp_password}</p>
                        </div>
                        <p>Log in at <a href="{settings.public_url}">{settings.public_url}</a> and change your password in Settings.</p>
                    </div>
                    """,
                },
            )
    except Exception:
        pass

    return {"message": "If that email exists, a reset link has been sent."}
