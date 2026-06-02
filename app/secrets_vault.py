"""
Per-tenant encrypted secrets vault.

Stores small string credentials (Twilio sub-account auth token, Resend
API key per tenant, etc.) in the `tenant_secrets` table, encrypted at
rest with Fernet (AES-128-CBC + HMAC-SHA256, key-rotation friendly).

Keys come from `TENANT_SECRETS_KEY` env var (32 random bytes, urlsafe
base64). If unset, we derive a key from SECRET_KEY via SHA-256 so a
fresh install works out of the box — but ops should set a dedicated
key in production and rotate it independently from JWT signing.

Usage:
    from app.secrets_vault import get_secret, set_secret

    twilio_token = await get_secret(db, tenant_id, "twilio_auth_token")
    await set_secret(db, tenant_id, "twilio_auth_token", "AC...new...")

The helpers accept a tenant_id explicitly rather than reading
session.info — secrets are sensitive enough that you should be looking
right at the tenant id you mean to read/write.
"""
from __future__ import annotations
import base64
import hashlib
import os
from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import TenantSecret


def _derive_fernet_key() -> bytes:
    """Resolve the Fernet key to use for encrypt/decrypt.

    Priority:
      1. TENANT_SECRETS_KEY env var (preferred — dedicated, rotatable).
         Must be 32 urlsafe-base64 bytes (i.e. a Fernet.generate_key() value).
      2. Fall back to SHA-256(settings.secret_key), urlsafe-b64 encoded.
         Lets a fresh install work; ties secret-encryption to JWT-signing key.
    """
    env = os.environ.get("TENANT_SECRETS_KEY")
    if env:
        return env.encode() if isinstance(env, str) else env

    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


_FERNET: Optional[Fernet] = None


def _fernet() -> Fernet:
    global _FERNET
    if _FERNET is None:
        _FERNET = Fernet(_derive_fernet_key())
    return _FERNET


def encrypt(plaintext: str) -> bytes:
    """Encrypt a string. Returns raw ciphertext bytes (BYTEA-safe)."""
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes) -> str:
    """Decrypt back to string. Raises cryptography.fernet.InvalidToken
    if the key has changed or the ciphertext is corrupt."""
    return _fernet().decrypt(bytes(ciphertext)).decode("utf-8")


async def get_secret(db: AsyncSession, tenant_id: int, name: str) -> Optional[str]:
    """Look up a secret by (tenant_id, name). Returns None if missing.

    Decryption errors propagate as InvalidToken — callers should treat
    them as "secret rotated under us" and fall back / reseed."""
    r = await db.execute(
        select(TenantSecret).where(
            TenantSecret.tenant_id == tenant_id,
            TenantSecret.name == name,
        )
    )
    row = r.scalar_one_or_none()
    if row is None:
        return None
    return decrypt(row.value_encrypted)


async def set_secret(db: AsyncSession, tenant_id: int, name: str, value: str) -> None:
    """Upsert a secret. Commits the change."""
    r = await db.execute(
        select(TenantSecret).where(
            TenantSecret.tenant_id == tenant_id,
            TenantSecret.name == name,
        )
    )
    row = r.scalar_one_or_none()
    if row is None:
        db.add(TenantSecret(
            tenant_id=tenant_id,
            name=name,
            value_encrypted=encrypt(value),
        ))
    else:
        row.value_encrypted = encrypt(value)
        row.updated_at = datetime.now(timezone.utc)
    await db.commit()


async def delete_secret(db: AsyncSession, tenant_id: int, name: str) -> bool:
    """Remove a secret. Returns True if a row was deleted."""
    r = await db.execute(
        select(TenantSecret).where(
            TenantSecret.tenant_id == tenant_id,
            TenantSecret.name == name,
        )
    )
    row = r.scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True
