"""Phone matching that survives format differences.

Twilio/webhooks hand us E.164 ("+14805551234"); the CRM stores phones in
Google's pretty format ("(480) 555-1234") — 0 of 3,268 company phones
were E.164 when audited 2026-06-10. Every exact-compare lookup in the
codebase therefore matched nothing: reconciliation stubs were orphaned,
callback voicemails dropped, inbound SMS (including STOP opt-outs!)
unattributed, inbound caller-ID never resolved.

Match by the last 10 digits instead. US/CA numbers only — that's the
entire prospect base; international prospect phones would need a smarter
comparison.
"""
from __future__ import annotations
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact, Company


def last10(raw: Optional[str]) -> Optional[str]:
    """Last 10 digits of a phone number, or None if it has fewer."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else None


def _scope_tid(db: AsyncSession, tenant_id: Optional[int]) -> Optional[int]:
    """Tenant to constrain the raw last-10 lookup to. Explicit arg wins;
    otherwise fall back to the session's tenant stamp (set by get_tenant_db).
    Returns None only for genuinely untenanted sessions — those stay
    cross-tenant by design (a few inbound webhooks resolve the owning tenant
    by the receiving number, not by a session stamp)."""
    if tenant_id is not None:
        return tenant_id
    try:
        return db.info.get("tenant_id")
    except Exception:
        return None


async def find_contact_by_phone(
    db: AsyncSession, raw: Optional[str], tenant_id: Optional[int] = None
) -> Optional[Contact]:
    """Contact whose phone matches by last-10 digits (primary first).

    The raw SQL bypasses the ORM tenant auto-filter, so it is scoped here:
    constrained to `tenant_id` (explicit, or the session's stamp). Without a
    tenant the match spans all tenants — only correct for system callers.
    """
    d = last10(raw)
    if d is None:
        return None
    tid = _scope_tid(db, tenant_id)
    sql = """
        SELECT id FROM contacts
        WHERE phone IS NOT NULL AND phone != ''
          AND RIGHT(regexp_replace(phone, '[^0-9]', '', 'g'), 10) = :d
    """
    params = {"d": d}
    if tid is not None:
        sql += " AND tenant_id = :t"
        params["t"] = tid
    sql += " ORDER BY is_primary DESC, id LIMIT 1"
    row = (await db.execute(text(sql), params)).first()
    if row is None:
        return None
    return (await db.execute(
        select(Contact).where(Contact.id == int(row.id))
    )).scalar_one_or_none()


async def find_company_by_phone(
    db: AsyncSession, raw: Optional[str], tenant_id: Optional[int] = None
) -> Optional[Company]:
    """Company whose main line matches by last-10 digits. Tenant-scoped like
    find_contact_by_phone — see that docstring."""
    d = last10(raw)
    if d is None:
        return None
    tid = _scope_tid(db, tenant_id)
    sql = """
        SELECT id FROM companies
        WHERE phone IS NOT NULL AND phone != ''
          AND RIGHT(regexp_replace(phone, '[^0-9]', '', 'g'), 10) = :d
    """
    params = {"d": d}
    if tid is not None:
        sql += " AND tenant_id = :t"
        params["t"] = tid
    sql += " ORDER BY id LIMIT 1"
    row = (await db.execute(text(sql), params)).first()
    if row is None:
        return None
    return (await db.execute(
        select(Company).where(Company.id == int(row.id))
    )).scalar_one_or_none()
