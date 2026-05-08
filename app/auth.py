from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.database import get_db
from app.models import User

SECRET_KEY = settings.secret_key
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if sub is None:
            raise credentials_exception
        user_id = int(sub)
    except JWTError:
        raise credentials_exception

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exception
    return user


async def require_super_admin(user: User = Depends(get_current_user)) -> User:
    """Only super_admin can access — API keys, runtime config, billing."""
    if user.role != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """Admins and super_admins can access — user management, campaigns, global view."""
    if user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def get_user_from_api_key(
    request: Request = None,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Authenticate a request via the X-API-Key header. Used on the
    public /api/v1/* surface. The plaintext key arrives in the header,
    we hash it with SHA-256 and look up the matching api_keys row."""
    from fastapi import Request as _R, HTTPException as _HE
    from app.models import ApiKey
    import hashlib
    if request is None:
        raise _HE(status_code=401, detail="Missing X-API-Key header")
    key = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
    if not key or not key.strip():
        raise _HE(status_code=401, detail="Missing X-API-Key header", headers={"WWW-Authenticate": "ApiKey"})
    key_hash = hashlib.sha256(key.strip().encode()).hexdigest()
    row = (await db.execute(
        select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active == True)
    )).scalar_one_or_none()
    if not row:
        raise _HE(status_code=401, detail="Invalid or revoked API key", headers={"WWW-Authenticate": "ApiKey"})
    user_row = (await db.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    if not user_row or not user_row.is_active:
        raise _HE(status_code=401, detail="API key owner is inactive")
    # Stamp last_used_at lazily — fire-and-forget so it doesn't block hot-path
    try:
        row.last_used_at = datetime.now(timezone.utc)
        await db.commit()
    except Exception:
        pass
    return user_row


async def require_sales_rep(user: User = Depends(get_current_user)) -> User:
    """Sales reps, admins, and super_admins can access. Read-only cannot."""
    if user.role == "read_only":
        raise HTTPException(status_code=403, detail="Read-only accounts cannot perform this action")
    return user


# ============================================================
# Role-escalation guards
# ============================================================
# Admins manage operations: users, sequences, tenant settings.
# Super_admins manage platform infrastructure: API keys, billing, the
# admin layer above admins. The rules below stop an admin from
# promoting themselves, modifying a super_admin, or otherwise climbing
# the ladder.

ROLE_ASSIGNABLE = {
    "super_admin": {"super_admin", "admin", "sales_rep", "read_only"},
    "admin":       {"admin", "sales_rep", "read_only"},  # NO super_admin
    "sales_rep":   set(),
    "read_only":   set(),
}


def role_assignable_by(actor: User) -> set[str]:
    """Which roles can `actor` assign to other users?"""
    return ROLE_ASSIGNABLE.get(actor.role, set())


def can_modify_user(actor: User, target: User) -> tuple[bool, str]:
    """Can `actor` modify `target` (role / sending / active flags / delete)?

    Rules:
      - super_admin can modify anyone
      - admin can modify non-super_admin users only
      - everyone else: cannot modify other users
    """
    if actor.id == target.id and actor.role in ("admin", "super_admin"):
        # Self-edit allowed for own profile fields; the caller is responsible
        # for blocking self-demotion / self-deletion separately.
        return True, ""
    if actor.role == "super_admin":
        return True, ""
    if actor.role == "admin":
        if target.role == "super_admin":
            return False, "Admins cannot modify a super admin account"
        return True, ""
    return False, "Insufficient privilege"
