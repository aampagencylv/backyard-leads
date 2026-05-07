"""
Multi-tenant query scoping.

Sales reps see only their assigned companies, contacts, deals, and tasks.
Admins see everything with optional rep filter.

Usage:
    query = select(Company)
    query = scope_companies(query, user)
    # Sales rep: adds WHERE assigned_to = user.id
    # Admin: no filter (or filter by rep_id if provided)
"""
from __future__ import annotations
from typing import Optional
from sqlalchemy import select
from app.models import User, Company, Contact, Deal, Task


def scope_companies(query, user: User, rep_id: Optional[int] = None):
    """Scope company queries. Reps see only their companies. Admins see all."""
    if user.role in ("admin", "super_admin"):
        if rep_id:
            return query.where(Company.assigned_to == rep_id)
        return query
    return query.where(Company.assigned_to == user.id)


def scope_contacts(query, user: User, rep_id: Optional[int] = None):
    """Scope contact queries via company ownership."""
    if user.role in ("admin", "super_admin"):
        if rep_id:
            return query.where(Company.assigned_to == rep_id)
        return query
    return query.where(Company.assigned_to == user.id)


def scope_deals(query, user: User, rep_id: Optional[int] = None):
    """Scope deal queries. Reps see only their deals."""
    if user.role in ("admin", "super_admin"):
        if rep_id:
            return query.where(Deal.assigned_to == rep_id)
        return query
    return query.where(Deal.assigned_to == user.id)


def scope_tasks(query, user: User, rep_id: Optional[int] = None):
    """Scope task queries. Reps see only their tasks."""
    if user.role in ("admin", "super_admin"):
        if rep_id:
            return query.where(Task.user_id == rep_id)
        return query
    return query.where(Task.user_id == user.id)


def check_company_access(company: Company, user: User) -> bool:
    """Return True if user can access this company."""
    if user.role in ("admin", "super_admin"):
        return True
    return company.assigned_to == user.id


def check_deal_access(deal: Deal, user: User) -> bool:
    """Return True if user can access this deal."""
    if user.role in ("admin", "super_admin"):
        return True
    return deal.assigned_to == user.id


async def check_contact_access(contact: Contact, user: User, db) -> bool:
    """Return True if user can access this contact (via company ownership)."""
    if user.role in ("admin", "super_admin"):
        return True
    from sqlalchemy import select as _sel
    company = (await db.execute(_sel(Company).where(Company.id == contact.company_id))).scalar_one_or_none()
    if not company:
        return False
    return company.assigned_to == user.id
