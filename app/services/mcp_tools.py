"""
MCP tool layer.

These functions are the actual implementations behind both:
  - the MCP server at /mcp (external AI clients — Claude Desktop,
    Claude.ai, ChatGPT with MCP, etc.)
  - the in-app AI chatbot widget (Anthropic tool-use, same surface)

Each tool:
  - Takes (db, user, **params) and returns a JSON-serializable dict
  - Is multi-tenant scoped via scope_companies / scope_contacts so
    a sales_rep only sees their own data, admins see all
  - Has a paired entry in TOOL_DEFINITIONS describing the JSON-schema
    inputs the AI should use to call it

The schema definitions are kept in this file (not split into a
separate manifest) so a tool's behavior + its contract live next
to each other.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity, Company, Contact, Deal, User
from app.scoping import scope_companies, scope_contacts

log = logging.getLogger("bmp.mcp_tools")


# Maximum rows we'll ever return on a single tool call. AI clients
# often pull more than they need; capping prevents accidental "give
# me everything" requests from blowing up token budgets.
HARD_LIMIT = 50


def _company_summary(c: Company, *, primary_contact: Optional[Contact] = None) -> dict:
    """Lightweight company shape for list responses. The full record
    (with timeline, deals, intel) lives behind get_company."""
    return {
        "id": c.id,
        "name": c.name,
        "website": c.website,
        "domain": c.domain,
        "city": c.city,
        "state": c.state,
        "phone": c.phone,
        "industry": c.industry,
        "business_type": c.business_type,
        "status": c.status,
        "rating": c.rating,
        "review_count": c.review_count,
        "lead_score": c.lead_score,
        "lead_score_tier": c.lead_score_tier,
        "assigned_to": c.assigned_to,
        "monthly_visits": c.monthly_visits,
        "primary_contact": (
            {
                "id": primary_contact.id,
                "name": primary_contact.full_name,
                "title": primary_contact.title,
                "email": primary_contact.email,
                "phone": primary_contact.phone,
            }
            if primary_contact else None
        ),
    }


def _contact_summary(c: Contact, *, company_name: str = "") -> dict:
    return {
        "id": c.id,
        "name": c.full_name,
        "first_name": c.first_name,
        "last_name": c.last_name,
        "title": c.title,
        "email": c.email,
        "phone": c.phone,
        "linkedin_url": c.linkedin_url,
        "is_primary": bool(c.is_primary),
        "phone_type": c.phone_type,
        "email_status": c.email_status,
        "company_id": c.company_id,
        "company_name": company_name or None,
    }


# ============================================================
# Tools
# ============================================================

async def search_companies(
    db: AsyncSession, user: User, *,
    query: Optional[str] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
    status: Optional[str] = None,
    business_type: Optional[str] = None,
    min_score: Optional[int] = None,
    limit: int = 25,
) -> dict:
    limit = max(1, min(HARD_LIMIT, limit or 25))
    stmt = scope_companies(select(Company), user, None)
    if query:
        pat = f"%{query.strip()}%"
        stmt = stmt.where(or_(Company.name.ilike(pat), Company.website.ilike(pat)))
    if city:
        stmt = stmt.where(Company.city.ilike(f"%{city.strip()}%"))
    if state:
        stmt = stmt.where(Company.state == state.strip().upper())
    if status:
        stmt = stmt.where(Company.status == status.strip().lower())
    if business_type:
        stmt = stmt.where(Company.business_type.ilike(f"%{business_type.strip()}%"))
    if min_score is not None:
        stmt = stmt.where(Company.lead_score >= int(min_score))
    stmt = stmt.order_by(desc(Company.lead_score), desc(Company.updated_at)).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "count": len(rows),
        "companies": [_company_summary(c) for c in rows],
    }


async def get_company(
    db: AsyncSession, user: User, *, company_id: int,
) -> dict:
    """Full record. Includes contacts, deals, last 20 activities,
    and the cached enrichment payloads (insights, similarweb, sos)."""
    company = (await db.execute(
        select(Company).where(Company.id == int(company_id))
    )).scalar_one_or_none()
    if not company:
        return {"error": "not_found"}
    from app.scoping import check_company_access
    if not await check_company_access(company, user, db):
        return {"error": "not_found"}

    contacts = (await db.execute(
        select(Contact).where(Contact.company_id == company.id)
        .order_by(desc(Contact.is_primary), Contact.id)
    )).scalars().all()
    deals = (await db.execute(
        select(Deal).where(Deal.company_id == company.id).order_by(desc(Deal.id))
    )).scalars().all()
    activities = (await db.execute(
        select(Activity).where(Activity.company_id == company.id)
        .order_by(desc(Activity.created_at)).limit(20)
    )).scalars().all()

    return {
        "id": company.id,
        "name": company.name,
        "website": company.website,
        "domain": company.domain,
        "phone": company.phone,
        "address": getattr(company, "address", None),
        "city": company.city,
        "state": company.state,
        "industry": company.industry,
        "business_type": company.business_type,
        "status": company.status,
        "rating": company.rating,
        "review_count": company.review_count,
        "employee_count": company.employee_count,
        "linkedin_url": company.linkedin_url,
        "facebook_url": company.facebook_url,
        "instagram_url": company.instagram_url,
        "youtube_url": company.youtube_url,
        "tiktok_url": company.tiktok_url,
        "lead_score": company.lead_score,
        "lead_score_tier": company.lead_score_tier,
        "monthly_visits": company.monthly_visits,
        "company_insights": (
            json.loads(company.company_insights_json) if company.company_insights_json else None
        ),
        "similarweb": (
            json.loads(company.similarweb_json) if company.similarweb_json else None
        ),
        "tech_stack": (
            json.loads(company.tech_stack_json) if company.tech_stack_json else None
        ),
        "contacts": [_contact_summary(c, company_name=company.name) for c in contacts],
        "deals": [
            {
                "id": d.id,
                "name": d.name,
                "stage": d.stage,
                "value": float(d.value) if d.value is not None else None,
                "probability": d.probability,
                "package_label": getattr(d, "package_label", None),
                "lost_reason": getattr(d, "lost_reason", None),
            }
            for d in deals
        ],
        "recent_activity": [
            {
                "type": a.activity_type,
                "content": (a.content or "")[:300],
                "user_id": a.user_id,
                "contact_id": a.contact_id,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in activities
        ],
    }


async def search_contacts(
    db: AsyncSession, user: User, *,
    query: Optional[str] = None,
    company_id: Optional[int] = None,
    has_email: Optional[bool] = None,
    has_phone: Optional[bool] = None,
    limit: int = 25,
) -> dict:
    limit = max(1, min(HARD_LIMIT, limit or 25))
    stmt = scope_contacts(
        select(Contact, Company.name).join(Company, Contact.company_id == Company.id),
        user, None,
    )
    if query:
        pat = f"%{query.strip()}%"
        stmt = stmt.where(or_(
            (Contact.first_name + " " + Contact.last_name).ilike(pat),
            Contact.email.ilike(pat),
            Company.name.ilike(pat),
        ))
    if company_id is not None:
        stmt = stmt.where(Contact.company_id == int(company_id))
    if has_email is True:
        stmt = stmt.where(Contact.email.isnot(None), Contact.email != "")
    elif has_email is False:
        stmt = stmt.where((Contact.email.is_(None)) | (Contact.email == ""))
    if has_phone is True:
        stmt = stmt.where(Contact.phone.isnot(None), Contact.phone != "")
    elif has_phone is False:
        stmt = stmt.where((Contact.phone.is_(None)) | (Contact.phone == ""))
    stmt = stmt.order_by(desc(Contact.is_primary), Contact.id).limit(limit)
    rows = (await db.execute(stmt)).all()
    return {
        "count": len(rows),
        "contacts": [
            _contact_summary(c, company_name=cname)
            for c, cname in rows
        ],
    }


async def get_contact(
    db: AsyncSession, user: User, *, contact_id: int,
) -> dict:
    contact = (await db.execute(
        select(Contact).where(Contact.id == int(contact_id))
    )).scalar_one_or_none()
    if not contact:
        return {"error": "not_found"}
    from app.scoping import check_contact_access
    if not await check_contact_access(contact, user, db):
        return {"error": "not_found"}
    company = (await db.execute(
        select(Company).where(Company.id == contact.company_id)
    )).scalar_one_or_none()
    activities = (await db.execute(
        select(Activity).where(Activity.contact_id == contact.id)
        .order_by(desc(Activity.created_at)).limit(15)
    )).scalars().all()
    return {
        **_contact_summary(contact, company_name=company.name if company else ""),
        "company": _company_summary(company) if company else None,
        "recent_activity": [
            {
                "type": a.activity_type,
                "content": (a.content or "")[:300],
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in activities
        ],
    }


async def get_pipeline_summary(
    db: AsyncSession, user: User,
) -> dict:
    """Stages × value × deal counts. Multi-tenant scoped — sales_reps
    see only their own assigned companies."""
    # Pull deals with their company so we can scope-filter
    stmt = scope_companies(
        select(Deal, Company).join(Company, Deal.company_id == Company.id),
        user, None,
    )
    rows = (await db.execute(stmt)).all()
    by_stage: dict[str, dict] = {}
    total = 0.0
    for d, _c in rows:
        s = (d.stage or "unknown").lower()
        if s not in by_stage:
            by_stage[s] = {"stage": s, "count": 0, "value": 0.0, "weighted_value": 0.0}
        by_stage[s]["count"] += 1
        v = float(d.value or 0)
        by_stage[s]["value"] += v
        by_stage[s]["weighted_value"] += v * (float(d.probability or 0) / 100.0)
        if s not in ("closed_lost",):
            total += v
    # Hot leads (last 30 min)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    hot_count = (await db.execute(
        scope_companies(
            select(func.count(func.distinct(Activity.company_id)))
            .join(Company, Activity.company_id == Company.id)
            .where(Activity.activity_type == "hot_lead", Activity.created_at >= cutoff),
            user, None,
        )
    )).scalar() or 0
    return {
        "stages": sorted(by_stage.values(), key=lambda x: -x["value"]),
        "total_open_value": total,
        "hot_leads_now": int(hot_count),
    }


async def find_hot_leads(
    db: AsyncSession, user: User, *,
    hours: int = 24,
    limit: int = 25,
) -> dict:
    """Companies that triggered hot_lead Activity (3+ opens, click,
    pageview burst, etc.) in the last N hours. Scoped to the rep."""
    hours = max(1, min(720, int(hours or 24)))
    limit = max(1, min(HARD_LIMIT, int(limit or 25)))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    stmt = scope_companies(
        select(Company, func.max(Activity.created_at).label("last_activity"))
        .join(Activity, Activity.company_id == Company.id)
        .where(Activity.activity_type == "hot_lead", Activity.created_at >= cutoff)
        .group_by(Company.id),
        user, None,
    ).order_by(desc("last_activity")).limit(limit)
    rows = (await db.execute(stmt)).all()
    return {
        "window_hours": hours,
        "count": len(rows),
        "hot_leads": [
            {**_company_summary(c), "last_hot_at": last.isoformat() if last else None}
            for c, last in rows
        ],
    }


async def get_recent_replies(
    db: AsyncSession, user: User, *,
    days: int = 7,
    sentiment: Optional[str] = None,
    limit: int = 25,
) -> dict:
    """Recent inbound replies (email_replied Activity). Surfaces the
    AI-classified sentiment so the AI can prioritize 'interested' over
    'objection' or 'OOO'."""
    days = max(1, min(60, int(days or 7)))
    limit = max(1, min(HARD_LIMIT, int(limit or 25)))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = scope_companies(
        select(Activity, Company.name)
        .join(Company, Activity.company_id == Company.id)
        .where(Activity.activity_type == "email_replied", Activity.created_at >= cutoff),
        user, None,
    ).order_by(desc(Activity.created_at)).limit(limit * 2)  # extra room for sentiment filter
    rows = (await db.execute(stmt)).all()
    out = []
    for a, cname in rows:
        meta = json.loads(a.metadata_json) if a.metadata_json else {}
        sent = meta.get("sentiment") or meta.get("reply_sentiment")
        if sentiment and (sent or "").lower() != sentiment.lower():
            continue
        out.append({
            "activity_id": a.id,
            "company_id": a.company_id,
            "company_name": cname,
            "contact_id": a.contact_id,
            "sentiment": sent,
            "summary": meta.get("sentiment_gist") or meta.get("gist"),
            "preview": (a.content or "")[:240],
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })
        if len(out) >= limit:
            break
    return {"count": len(out), "replies": out}


async def summarize_company(
    db: AsyncSession, user: User, *, company_id: int,
) -> dict:
    """AI-generated brief — short paragraph + 3 talking points. Cheap
    Sonnet call, ~$0.005. Pulls the same data get_company does and
    asks Claude to distill it."""
    full = await get_company(db, user, company_id=int(company_id))
    if "error" in full:
        return full
    # Lazy import to avoid pulling Anthropic SDK on every cold start
    from app.config import settings
    if not settings.anthropic_api_key:
        return {"error": "anthropic_not_configured", "fallback": _heuristic_brief(full)}
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        prompt = (
            "You are a B2B sales coach. Given this CRM record, write:\n"
            "  1) A 2-sentence brief explaining who this company is and "
            "why they're a worth-pursuing prospect (or why not).\n"
            "  2) Three concrete talking points the rep can open a call "
            "with — each tied to a specific signal in the data, not generic.\n\n"
            "Return JSON: {\"summary\": str, \"talking_points\": [str, str, str]}.\n\n"
            f"DATA:\n{json.dumps(full, default=str)[:8000]}"
        )
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        # Best-effort JSON parse — model sometimes wraps in markdown
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            parsed = json.loads(text)
            return {"company_id": full["id"], "company_name": full["name"], **parsed}
        except json.JSONDecodeError:
            return {"company_id": full["id"], "company_name": full["name"], "summary": text}
    except Exception as e:
        log.warning(f"summarize_company AI call failed for company {company_id}: {e}")
        return {**_heuristic_brief(full), "error": "ai_call_failed"}


def _heuristic_brief(full: dict) -> dict:
    """No-AI fallback. Compose a brief from the structured fields."""
    bits = []
    if full.get("business_type"):
        bits.append(f"{full['business_type']}")
    if full.get("city") and full.get("state"):
        bits.append(f"in {full['city']}, {full['state']}")
    if full.get("rating") and full.get("review_count"):
        bits.append(f"{full['rating']}★ on {full['review_count']} reviews")
    if full.get("lead_score") is not None:
        bits.append(f"lead score {full['lead_score']}/100 ({full.get('lead_score_tier')})")
    summary = (
        f"{full.get('name', 'Company')} — "
        + " · ".join(bits) if bits else f"{full.get('name', 'Company')}"
    )
    talking = []
    insights = full.get("company_insights") or {}
    if insights.get("growth_signals"):
        talking.append(f"Growth signal: {insights['growth_signals'][0]}")
    if full.get("similarweb", {}).get("monthly_visits"):
        talking.append(f"~{full['similarweb']['monthly_visits']:,}/mo website visits")
    if full.get("contacts"):
        c0 = full["contacts"][0]
        talking.append(f"Primary contact: {c0.get('name')} ({c0.get('title') or 'role unknown'})")
    return {
        "company_id": full.get("id"),
        "company_name": full.get("name"),
        "summary": summary,
        "talking_points": talking[:3],
    }


# ============================================================
# Tool registry — name → (callable, JSON schema)
# ============================================================
#
# The schema descriptions are written for the AI, not for humans.
# Be specific about when to use each tool, what good queries look
# like, and what the response shape contains. Vague descriptions
# lead to AIs picking the wrong tool or constructing malformed calls.

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "search_companies",
        "description": (
            "Search the user's companies (B2B prospects) by name, city, "
            "state, status, business type, or lead score. Returns up to "
            "50 lightweight company summaries sorted by lead score then "
            "recency. Use this to find prospects matching criteria the "
            "user describes ('pool builders in Phoenix', 'qualified deals "
            "in Vegas with score >= 60'). For full detail call get_company."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query":         {"type": "string", "description": "Substring match on company name or website."},
                "city":          {"type": "string"},
                "state":         {"type": "string", "description": "Two-letter US state code (uppercased automatically)."},
                "status":        {"type": "string", "enum": ["new", "pursuing", "sequencing", "contacted", "qualified", "replied", "closed_won", "closed_lost"]},
                "business_type": {"type": "string", "description": "e.g. 'pool builder', 'landscaper', 'deck contractor'"},
                "min_score":     {"type": "integer", "minimum": 0, "maximum": 100, "description": "Lead score 0-100. Tier cutoffs: cool 20+, warm 40+, hot 60+, burning 80+."},
                "limit":         {"type": "integer", "minimum": 1, "maximum": 50, "default": 25},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_company",
        "description": (
            "Full company record: contacts, deals, last 20 activities, "
            "and cached enrichment payloads (firmographics, traffic stats, "
            "tech stack). Use after search_companies returns the id you "
            "want to drill into."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"company_id": {"type": "integer"}},
            "required": ["company_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_contacts",
        "description": (
            "Search contacts (people at companies) by name / email / "
            "company / or whether they have email/phone. Returns up to "
            "50 contact summaries. For the full record + activity history "
            "call get_contact."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query":      {"type": "string"},
                "company_id": {"type": "integer", "description": "Restrict to a specific company"},
                "has_email":  {"type": "boolean"},
                "has_phone":  {"type": "boolean"},
                "limit":      {"type": "integer", "minimum": 1, "maximum": 50, "default": 25},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_contact",
        "description": "Full contact record + last 15 activities + their company.",
        "inputSchema": {
            "type": "object",
            "properties": {"contact_id": {"type": "integer"}},
            "required": ["contact_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_pipeline_summary",
        "description": (
            "Pipeline health snapshot: deals grouped by stage, total open "
            "value, weighted value (×stage probability), and hot-lead "
            "count from the last 30 minutes. No inputs."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "find_hot_leads",
        "description": (
            "Companies that triggered hot_lead activity (3+ opens, any "
            "click, recent page-view burst) in the last N hours. Sorted "
            "by most recent activity. Use this for 'who should I call "
            "right now?' questions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "minimum": 1, "maximum": 720, "default": 24},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 25},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_recent_replies",
        "description": (
            "Recent inbound email replies awaiting human action. Each "
            "reply includes the AI-classified sentiment ('interested', "
            "'objection', 'not_now', 'ooo', etc.) and a one-line gist. "
            "Use sentiment filter to focus the user on hot replies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days":      {"type": "integer", "minimum": 1, "maximum": 60, "default": 7},
                "sentiment": {"type": "string", "description": "Filter to one sentiment label ('interested', 'objection', etc.)"},
                "limit":     {"type": "integer", "minimum": 1, "maximum": 50, "default": 25},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "summarize_company",
        "description": (
            "AI-generated 2-sentence brief + 3 concrete talking points "
            "for a company. Pulls the same data as get_company and asks "
            "Claude to distill it. Use when the user asks 'tell me about "
            "X' or wants pre-call prep."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"company_id": {"type": "integer"}},
            "required": ["company_id"],
            "additionalProperties": False,
        },
    },
]

TOOL_HANDLERS: dict[str, Any] = {
    "search_companies":     search_companies,
    "get_company":          get_company,
    "search_contacts":      search_contacts,
    "get_contact":          get_contact,
    "get_pipeline_summary": get_pipeline_summary,
    "find_hot_leads":       find_hot_leads,
    "get_recent_replies":   get_recent_replies,
    "summarize_company":    summarize_company,
}
