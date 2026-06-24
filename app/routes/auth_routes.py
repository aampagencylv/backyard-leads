from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel
from app.database import get_db
from app.tenancy import get_tenant_db, get_current_tenant_id
from app.models import User
from app.services.audit_log import record_audit
from app.auth import (
    hash_password, verify_password, create_access_token,
    get_current_user, require_admin,
    can_modify_user, role_assignable_by,
)

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
async def register(
    req: RegisterRequest,
    db: AsyncSession = Depends(get_tenant_db),
    tenant_id: int = Depends(get_current_tenant_id),
):
    """Disabled — tenants are provisioned via the platform admin console
    (POST /api/admin/tenants/{id}/users by a super_admin).

    Self-service registration was a single-tenant artifact: anyone with
    a @backyardmarketingpros.com or @aamp.agency email could create a
    BMP user. In a multi-tenant world that doesn't make sense — every
    tenant has its own user pool, and admission is gated by the
    platform admin who knows which agency they're inviting.
    """
    raise HTTPException(
        status_code=403,
        detail="Self-service registration is disabled. Contact your platform admin to be invited.",
    )


class UniversalLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_name: str
    user_email: str
    tenant_name: str
    redirect_url: str  # Full URL the browser should navigate to


@router.post("/universal-login", response_model=UniversalLoginResponse)
async def universal_login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),  # cross-tenant — no auto-filter
):
    """Email + password login that searches across every tenant.

    Powers the leadprospector.ai/login form. A user with the same email
    + password in any tenant gets matched; the response tells the
    browser where to redirect (their tenant's primary domain), with a
    JWT for that tenant carried in the URL so localStorage transfer
    across the subdomain hop works.

    Super_admin users redirect to app.leadprospector.ai/admin instead
    of their tenant — they're the platform operators.

    Same enumeration-protection as /login: identical 401 response for
    "wrong email" and "wrong password" so an attacker can't probe which
    emails exist on the platform.

    If the same email + password matches in multiple tenants (rare
    today; common once we add multi-tenant memberships), the first
    match wins. We'll add a "choose tenant" picker if + when this
    actually trips real users.
    """
    from app.models import TenantDomain
    email = form_data.username.strip().lower()
    raw_password = form_data.password

    # Cross-tenant user lookup — get every user with this email across
    # the platform. Don't bother filtering by password in SQL; bcrypt
    # is per-row anyway.
    candidates = (await db.execute(
        select(User).where(User.email == email, User.is_active == True)
    )).scalars().all()

    matched_user: Optional[User] = None
    for u in candidates:
        if verify_password(raw_password, u.hashed_password):
            matched_user = u
            break

    if matched_user is None:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Resolve the redirect URL.
    # 1. super_admin → app.leadprospector.ai/admin
    # 2. anyone else → their tenant's primary domain
    if matched_user.role == "super_admin":
        redirect_url = "https://app.leadprospector.ai/admin"
        tenant_name = "Platform admin"
    else:
        primary = (await db.execute(
            select(TenantDomain).where(
                TenantDomain.tenant_id == matched_user.tenant_id,
                TenantDomain.is_primary == True,
            ).limit(1)
        )).scalar_one_or_none()
        # Fallback: any verified domain for that tenant, then slug, then apex.
        if primary is None:
            primary = (await db.execute(
                select(TenantDomain).where(
                    TenantDomain.tenant_id == matched_user.tenant_id,
                    TenantDomain.is_verified == True,
                ).limit(1)
            )).scalar_one_or_none()
        host = primary.domain if primary else "app.leadprospector.ai"
        redirect_url = f"https://{host}/"
        # Tenant name comes from the Tenant row; cheap lookup.
        from app.models import Tenant as _T
        tenant_row = (await db.execute(
            select(_T).where(_T.id == matched_user.tenant_id)
        )).scalar_one_or_none()
        tenant_name = tenant_row.name if tenant_row else ""

    token = create_access_token({
        "sub": str(matched_user.id),
        "tenant_id": matched_user.tenant_id,
    })
    return UniversalLoginResponse(
        access_token=token,
        user_name=matched_user.full_name,
        user_email=matched_user.email,
        tenant_name=tenant_name,
        redirect_url=redirect_url,
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_tenant_db),
):
    # User lookup is auto-scoped to the resolved tenant by the ORM filter,
    # so a user logging in via tenantA.leadprospector.ai only matches
    # users belonging to tenantA — even if another tenant has the same email.
    email = form_data.username.lower()
    resolved_tid = db.info.get("tenant_id")
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user and verify_password(form_data.password, user.hashed_password):
        # Stamp tenant_id into the JWT so subsequent requests can resolve the
        # tenant without re-doing host lookup. The resolver checks JWT claim first.
        token = create_access_token({"sub": str(user.id), "tenant_id": user.tenant_id})
        return TokenResponse(
            access_token=token,
            user_name=user.full_name,
            user_email=user.email,
        )

    # Cross-tenant super_admin fallback: a platform super_admin has no user
    # row in most tenants, but should still be able to sign in directly at
    # any tenant subdomain (e.g. aamp.leadprospector.ai/login) and land
    # operating that tenant. We do an UNSCOPED lookup (a plain session has no
    # tenant stamp, so the ORM auto-filter doesn't apply) for an active
    # super_admin with this email+password, then mint a token that impersonates
    # the host-resolved tenant. Only super_admins get this; everyone else
    # falls through to the same 401 (no email enumeration).
    from app.database import async_session
    async with async_session() as gdb:
        candidates = (await gdb.execute(
            select(User).where(
                User.email == email,
                User.role == "super_admin",
                User.is_active == True,
            )
        )).scalars().all()
    for sa in candidates:
        if verify_password(form_data.password, sa.hashed_password):
            claims = {"sub": str(sa.id), "tenant_id": sa.tenant_id}
            # Only add acting_as when signing into a tenant that isn't the
            # super_admin's own home tenant.
            if resolved_tid and resolved_tid != sa.tenant_id:
                claims["acting_as_tenant_id"] = resolved_tid
            token = create_access_token(claims)
            return TokenResponse(
                access_token=token,
                user_name=sa.full_name,
                user_email=sa.email,
            )

    raise HTTPException(status_code=401, detail="Invalid email or password")


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
        "twilio_phone_number": user.twilio_phone_number,
        "twilio_identity": user.twilio_identity,
        "dial_mode": user.dial_mode or "browser",
        "is_available_for_calls": bool(getattr(user, 'is_available_for_calls', True)),
        "onboarding_step": user.onboarding_step,
        "brief_enabled": user.brief_enabled,
        "brief_hour": user.brief_hour,
        "timezone": user.timezone,
        "last_brief_sent_at": user.last_brief_sent_at.isoformat() if user.last_brief_sent_at else None,
    }


# ============================================================
# Morning brief — settings + preview + test-send
# ============================================================

class UpdateBriefSettingsRequest(BaseModel):
    brief_enabled: Optional[bool] = None
    brief_hour: Optional[int] = None
    timezone: Optional[str] = None


@router.patch("/me/brief-settings")
async def update_brief_settings(
    req: UpdateBriefSettingsRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Update the current user's morning brief preferences."""
    if req.brief_enabled is not None:
        user.brief_enabled = bool(req.brief_enabled)
    if req.brief_hour is not None:
        h = int(req.brief_hour)
        if 0 <= h <= 23:
            user.brief_hour = h
    if req.timezone is not None:
        # Validate it's a known IANA tz name
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(req.timezone)
            user.timezone = req.timezone
        except Exception:
            raise HTTPException(status_code=400, detail=f"Unknown timezone: {req.timezone}")
    await db.commit()
    return {
        "brief_enabled": user.brief_enabled,
        "brief_hour": user.brief_hour,
        "timezone": user.timezone,
    }


@router.post("/me/brief/test-send")
async def test_send_brief(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Force-send a morning brief to the current user, ignoring the
    'last_brief_sent_at' idempotency check. Used by the Settings UI's
    "Send a test now" button."""
    from app.services.morning_brief import send_brief
    # Temporarily clear the stamp so send_brief doesn't no-op, then
    # restore. Our actual check is in run_morning_brief_tick (cron path);
    # send_brief itself doesn't check, so we just call it.
    ok = await send_brief(db, user)
    return {"sent": ok}


class UpdateOnboardingRequest(BaseModel):
    step: int  # 0-10, 99 (skipped), or 100 (completed)


@router.patch("/me/onboarding")
async def update_onboarding(
    req: UpdateOnboardingRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Save the user's progress through the 10-step product tour.
    Step values: 0=not started, 1-10=in progress, 99=skipped, 100=completed."""
    if req.step < 0 or req.step > 100:
        raise HTTPException(status_code=400, detail="step must be 0-100")
    user.onboarding_step = req.step
    await db.commit()
    return {"onboarding_step": user.onboarding_step}


@router.post("/me/onboarding/restart")
async def restart_onboarding(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Reset onboarding so the tour fires again next login (or immediately if
    the frontend polls). Used by the 'Restart Tour' button in Settings."""
    user.onboarding_step = 0
    await db.commit()
    return {"onboarding_step": 0}


# ============ Admin: User Management ============

@router.get("/users")
async def list_users(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_admin),
):
    """List all users (admin only).

    Each row carries a `locked` flag when the current user can't modify
    that target — admins see super_admin accounts but can't edit them.
    """
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
            # True when the requesting user does NOT have privilege to
            # modify this row. UI should show a lock icon + disable buttons.
            "locked": not can_modify_user(user, u)[0],
            # Booking routing — when set, the BDR's outbound links book
            # on the host's calendar rather than their own.
            "default_booking_host_id": u.default_booking_host_id,
            "has_google_calendar": bool(u.google_refresh_token and u.booking_slug),
        }
        for u in users
    ]


class UpdateUserRoleRequest(BaseModel):
    role: str  # admin, sales_rep, read_only


@router.patch("/users/{user_id}/role")
async def update_user_role(
    user_id: int,
    req: UpdateUserRoleRequest,
    request: Request,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_admin),
):
    """Change a user's role.
    Admins cannot modify super_admins, cannot grant the super_admin role,
    and cannot demote the last super_admin (which would lock the system
    out of platform settings)."""
    if req.role not in ("super_admin", "admin", "senior_rep", "sales_rep", "read_only"):
        raise HTTPException(status_code=400, detail="Invalid role")

    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    allowed, reason = can_modify_user(user, target)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)
    if req.role not in role_assignable_by(user):
        raise HTTPException(status_code=403, detail=f"You don't have permission to assign the '{req.role}' role")

    # Don't strand the system without a super_admin.
    if target.role == "super_admin" and req.role != "super_admin":
        remaining = (await db.execute(
            select(func.count()).select_from(User)
            .where(User.role == "super_admin", User.id != target.id, User.is_active == True)
        )).scalar() or 0
        if remaining == 0:
            raise HTTPException(status_code=400, detail="Cannot demote the last active super admin")

    old_role = target.role
    target.role = req.role
    await record_audit(
        db, actor=user, action="user.role_changed",
        target_type="user", target_id=target.id, target_label=target.email,
        metadata={"from": old_role, "to": req.role}, request=request,
    )
    await db.commit()
    return {"id": target.id, "name": target.full_name, "role": target.role}


class InviteUserRequest(BaseModel):
    email: str
    first_name: str
    last_name: str
    role: str = "sales_rep"
    title: Optional[str] = None
    timezone: Optional[str] = None


@router.post("/users/invite")
async def invite_user(
    req: InviteUserRequest,
    request: Request,
    db: AsyncSession = Depends(get_tenant_db),
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

    if req.role not in ("super_admin", "admin", "senior_rep", "sales_rep", "read_only"):
        raise HTTPException(status_code=400, detail="Invalid role")

    if req.role not in role_assignable_by(user):
        raise HTTPException(status_code=403, detail=f"You don't have permission to create a user with the '{req.role}' role")

    # Generate a temporary password
    import secrets
    temp_password = secrets.token_urlsafe(12)

    # Validate timezone if provided
    user_tz = "America/Phoenix"  # default
    if req.timezone:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(req.timezone)
            user_tz = req.timezone
        except (KeyError, Exception):
            pass  # fall back to default

    new_user = User(
        email=req.email.lower(),
        first_name=req.first_name.strip(),
        last_name=req.last_name.strip(),
        nickname=req.title or "",
        hashed_password=hash_password(temp_password),
        role=req.role,
        timezone=user_tz,
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
            from app.services.html_to_text import html_to_plain_text
            welcome_html = f"""
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
                    """
            await httpx.AsyncClient(timeout=10).post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key}", "Content-Type": "application/json"},
                json={
                    "from": f"Backyard Marketing Pros <noreply@{settings.send_domain}>",
                    "to": [new_user.email],
                    "subject": "Welcome to BMP Prospector — Your Account is Ready",
                    "html": welcome_html,
                    "text": html_to_plain_text(welcome_html),
                },
            )
            email_sent = True
    except Exception:
        pass

    await record_audit(
        db, actor=user, action="user.invited",
        target_type="user", target_id=new_user.id, target_label=new_user.email,
        metadata={"role": new_user.role, "welcome_email_sent": email_sent}, request=request,
    )
    await db.commit()
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
    # default_booking_host_id explicit None vs missing — to clear the
    # routing the client sends 0 (sentinel "own calendar").
    default_booking_host_id: Optional[int] = None


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int,
    req: UpdateUserRequest,
    request: Request,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_admin),
):
    """Update any user's profile.
    Admins cannot modify super_admins, cannot grant super_admin via this
    endpoint, and cannot demote the last active super_admin."""
    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    allowed, reason = can_modify_user(user, target)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)

    if req.first_name is not None:
        target.first_name = req.first_name
    if req.last_name is not None:
        target.last_name = req.last_name
    if req.nickname is not None:
        target.nickname = req.nickname
    if req.role is not None:
        if req.role not in ("super_admin", "admin", "sales_rep", "read_only"):
            raise HTTPException(status_code=400, detail="Invalid role")
        if req.role not in role_assignable_by(user):
            raise HTTPException(status_code=403, detail=f"You don't have permission to assign the '{req.role}' role")
        if target.role == "super_admin" and req.role != "super_admin":
            remaining = (await db.execute(
                select(func.count()).select_from(User)
                .where(User.role == "super_admin", User.id != target.id, User.is_active == True)
            )).scalar() or 0
            if remaining == 0:
                raise HTTPException(status_code=400, detail="Cannot demote the last active super admin")
        target.role = req.role
    if req.sending_enabled is not None:
        target.sending_enabled = req.sending_enabled
    if req.is_active is not None:
        # Don't deactivate the last active super_admin
        if target.role == "super_admin" and req.is_active is False:
            remaining = (await db.execute(
                select(func.count()).select_from(User)
                .where(User.role == "super_admin", User.id != target.id, User.is_active == True)
            )).scalar() or 0
            if remaining == 0:
                raise HTTPException(status_code=400, detail="Cannot deactivate the last active super admin")
        target.is_active = req.is_active
    if req.default_booking_host_id is not None:
        # 0 = sentinel for "use their own calendar" (clears the field)
        if req.default_booking_host_id == 0 or req.default_booking_host_id == target.id:
            target.default_booking_host_id = None
        else:
            host_check = (await db.execute(
                select(User).where(User.id == req.default_booking_host_id, User.is_active == True)
            )).scalar_one_or_none()
            if not host_check:
                raise HTTPException(status_code=400, detail="Booking host not found or inactive")
            if not host_check.google_refresh_token:
                raise HTTPException(status_code=400, detail="Booking host hasn't connected Google Calendar yet")
            target.default_booking_host_id = host_check.id

    # Audit summary — only logs the fields the request actually touched
    changed = {}
    for field_name in ("first_name", "last_name", "nickname", "role", "sending_enabled", "is_active", "default_booking_host_id"):
        val = getattr(req, field_name)
        if val is not None:
            changed[field_name] = val
    if changed:
        await record_audit(
            db, actor=user, action="user.updated",
            target_type="user", target_id=target.id, target_label=target.email,
            metadata=changed, request=request,
        )
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
    db: AsyncSession = Depends(get_tenant_db),
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
    request: Request,
    db: AsyncSession = Depends(get_tenant_db),
):
    """Mint a one-time password-reset token + email it as a link.

    The user's existing password stays valid until they actually click
    the link and submit a new one. This avoids the lockout pattern
    where any visitor can rotate someone's password by spamming
    /forgot-password.

    Email enumeration is mitigated by always returning the same 200
    response whether or not the email exists.
    """
    from app.auth import mint_password_reset_token
    from app.services.platform_mailer import send_platform_email

    user = (await db.execute(
        select(User).where(User.email == req.email.strip().lower())
    )).scalar_one_or_none()

    if user:
        token = mint_password_reset_token(user.id)
        # Reset URL points at the SAME host the user came from — so a
        # tenant user clicks back into their own subdomain, not the
        # platform apex.
        host = (request.headers.get("host") or "app.leadprospector.ai").split(":")[0]
        reset_url = f"https://{host}/reset-password?token={token}"
        # Best-effort send. If the platform mailer isn't configured the
        # link won't reach the user — they can ask their admin for a
        # reset via the audit log.
        await send_platform_email(
            to=user.email,
            template="password_reset",
            vars={
                "first_name": user.first_name or user.email.split("@")[0],
                "reset_url": reset_url,
            },
        )

    return {"message": "If that email exists, a reset link has been sent."}


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


@router.post("/reset-password")
async def reset_password(
    req: ResetPasswordRequest,
    db: AsyncSession = Depends(get_tenant_db),
):
    """Consume a password-reset token + set a new password.

    Cross-tenant lookup: the user could be in any tenant; the token
    carries the user_id. We look up across tenants by clearing the
    session.info scope just for this lookup.
    """
    from app.auth import verify_password_reset_token

    user_id = verify_password_reset_token(req.token)
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")

    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    # User lookup must be cross-tenant — the token's user might live in
    # a different tenant than the host the reset page was loaded from.
    prev_tid = db.info.pop("tenant_id", None)
    try:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    finally:
        if prev_tid is not None:
            db.info["tenant_id"] = prev_tid

    if not user or not user.is_active:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")

    user.hashed_password = hash_password(req.new_password)
    await db.commit()

    # Issue a fresh JWT so the client can sign in immediately without
    # bouncing back through /login (and so the reset link can't be replayed).
    token = create_access_token({"sub": str(user.id), "tenant_id": user.tenant_id})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_name": user.full_name,
        "user_email": user.email,
    }


# ============ Audit log (admin+ visible) ============

@router.get("/audit-log")
async def list_audit_log(
    limit: int = 100,
    offset: int = 0,
    actor_user_id: Optional[int] = None,
    action: Optional[str] = None,
    target_type: Optional[str] = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_admin),
):
    """List audit log entries, newest first. Admin+ only.
    Filter knobs let admins answer 'what did Linda do this week' /
    'who changed a role' / 'all runtime_config edits' without grep."""
    from app.models import AuditLogEntry
    q = select(AuditLogEntry).order_by(AuditLogEntry.created_at.desc())
    if actor_user_id is not None:
        q = q.where(AuditLogEntry.actor_user_id == actor_user_id)
    if action:
        q = q.where(AuditLogEntry.action == action)
    if target_type:
        q = q.where(AuditLogEntry.target_type == target_type)
    q = q.limit(min(max(limit, 1), 500)).offset(max(offset, 0))
    rows = (await db.execute(q)).scalars().all()
    import json as _json
    return [
        {
            "id": r.id,
            "actor_user_id": r.actor_user_id,
            "actor_email": r.actor_email,
            "actor_role": r.actor_role,
            "action": r.action,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "target_label": r.target_label,
            "metadata": _json.loads(r.metadata_json) if r.metadata_json else None,
            "ip_address": r.ip_address,
            "user_agent": r.user_agent,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ============ BDR Reassignment ============

class ReassignRequest(BaseModel):
    from_user_id: int
    to_user_id: int


@router.post("/users/reassign")
async def reassign_user(
    req: ReassignRequest,
    request: Request,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_admin),
):
    """Bulk reassign all companies, deals, and tasks from one user to another."""
    from app.models import Company, Deal, Task
    from sqlalchemy import update

    from_user = (await db.execute(select(User).where(User.id == req.from_user_id))).scalar_one_or_none()
    to_user = (await db.execute(select(User).where(User.id == req.to_user_id))).scalar_one_or_none()
    if not from_user:
        raise HTTPException(status_code=404, detail="Source user not found")
    if not to_user:
        raise HTTPException(status_code=404, detail="Target user not found")

    # Admins cannot redistribute work owned by a super_admin.
    allowed, reason = can_modify_user(user, from_user)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)

    companies_result = await db.execute(
        update(Company).where(Company.assigned_to == req.from_user_id).values(assigned_to=req.to_user_id)
    )
    deals_result = await db.execute(
        update(Deal).where(
            Deal.assigned_to == req.from_user_id,
            Deal.stage.notin_(["closed_won", "closed_lost"]),
        ).values(assigned_to=req.to_user_id)
    )
    tasks_result = await db.execute(
        update(Task).where(
            Task.user_id == req.from_user_id,
            Task.completed == False,
        ).values(user_id=req.to_user_id)
    )

    await db.commit()

    await record_audit(
        db, actor=user, action="user.reassigned",
        target_type="user", target_id=from_user.id, target_label=from_user.email,
        metadata={
            "to_user_id": to_user.id,
            "to_email": to_user.email,
            "companies_moved": companies_result.rowcount,
            "deals_moved": deals_result.rowcount,
            "tasks_moved": tasks_result.rowcount,
        }, request=request,
    )
    await db.commit()
    return {
        "from": from_user.full_name,
        "to": to_user.full_name,
        "companies_moved": companies_result.rowcount,
        "deals_moved": deals_result.rowcount,
        "tasks_moved": tasks_result.rowcount,
    }
