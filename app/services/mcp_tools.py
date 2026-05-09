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

from app.models import Activity, Company, Contact, Deal, GeneratedEmail, User
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


# Tools that mutate state require an API key with scope='write'.
# Listed by name so the MCP handler can gate without inspecting tool
# metadata directly.
WRITE_TOOL_NAMES: set[str] = set()


# ============================================================
# Write tools (MCP v2a)
# ============================================================
#
# Mutating tools — each one writes audit + (where applicable) fires
# a webhook event so external systems stay in sync. These require
# scope='write' on the calling API key. Read-only keys get a 403.
#
# We don't bake confirmation prompts into the server itself; modern
# MCP clients (Claude Desktop, Claude.ai) automatically ask the user
# to confirm tool calls that look like writes. Belt-and-suspenders:
# every write here is also captured in the audit log so anything an
# AI does is reviewable after the fact.


async def _company_for_write(db: AsyncSession, user: User, company_id: int) -> Optional[Company]:
    """Resolve + access-check a company for a write tool. Returns None
    if not found / out of scope; caller should respond with an error."""
    company = (await db.execute(
        select(Company).where(Company.id == int(company_id))
    )).scalar_one_or_none()
    if not company:
        return None
    from app.scoping import check_company_access
    if not await check_company_access(company, user, db):
        return None
    return company


async def _contact_for_write(db: AsyncSession, user: User, contact_id: int) -> Optional[Contact]:
    contact = (await db.execute(
        select(Contact).where(Contact.id == int(contact_id))
    )).scalar_one_or_none()
    if not contact:
        return None
    from app.scoping import check_contact_access
    if not await check_contact_access(contact, user, db):
        return None
    return contact


async def add_note(
    db: AsyncSession, user: User, *,
    company_id: int, content: str, contact_id: Optional[int] = None,
) -> dict:
    """Log a free-form note Activity on a company timeline. The most
    common write operation — Claude can capture call notes, action
    items, or context the rep mentioned in chat."""
    company = await _company_for_write(db, user, int(company_id))
    if not company:
        return {"error": "company_not_found_or_out_of_scope"}
    txt = (content or "").strip()
    if not txt:
        return {"error": "content_required"}
    activity = Activity(
        company_id=company.id,
        contact_id=contact_id,
        user_id=user.id,
        activity_type="note",
        content=txt[:5000],
        metadata_json=json.dumps({"source": "mcp"}),
    )
    db.add(activity)
    from app.services.audit_log import record_audit
    await record_audit(
        db, actor=user, action="note.added_via_mcp",
        target_type="company", target_id=company.id, target_label=company.name,
        metadata={"content_preview": txt[:120], "contact_id": contact_id},
    )
    await db.commit()
    await db.refresh(activity)
    return {
        "ok": True,
        "activity_id": activity.id,
        "company_id": company.id,
        "company_name": company.name,
        "content": txt[:5000],
    }
WRITE_TOOL_NAMES.add("add_note")


async def create_task(
    db: AsyncSession, user: User, *,
    company_id: int, description: str,
    due_in_hours: Optional[int] = None,
    assignee_user_id: Optional[int] = None,
) -> dict:
    """Create a Task for the rep. `due_in_hours` is relative to now —
    e.g. 24 = tomorrow, 168 = in a week. If omitted, task has no due
    date. assignee_user_id defaults to the calling user."""
    from app.models import Task
    company = await _company_for_write(db, user, int(company_id))
    if not company:
        return {"error": "company_not_found_or_out_of_scope"}
    desc = (description or "").strip()
    if not desc:
        return {"error": "description_required"}
    due = None
    if due_in_hours and due_in_hours > 0:
        due = datetime.now(timezone.utc) + timedelta(hours=int(due_in_hours))
    assignee = int(assignee_user_id) if assignee_user_id else user.id
    task = Task(
        user_id=assignee,
        company_id=company.id,
        description=desc[:2000],
        due_date=due,
    )
    db.add(task)
    from app.services.audit_log import record_audit
    await record_audit(
        db, actor=user, action="task.created_via_mcp",
        target_type="company", target_id=company.id, target_label=company.name,
        metadata={"description": desc[:120], "due_in_hours": due_in_hours, "assignee_user_id": assignee},
    )
    await db.commit()
    await db.refresh(task)
    return {
        "ok": True,
        "task_id": task.id,
        "company_id": company.id,
        "description": desc[:2000],
        "due_date": due.isoformat() if due else None,
        "assignee_user_id": assignee,
    }
WRITE_TOOL_NAMES.add("create_task")


# Fields safe to PATCH on Company via MCP. Excludes id, status,
# scoring outputs, raw enrichment cache, etc. — those have their own
# tools or shouldn't be touched by external AI.
_COMPANY_WRITE_FIELDS = {
    "name", "phone", "address", "city", "state", "industry",
    "business_type", "website", "linkedin_url", "facebook_url",
    "instagram_url", "youtube_url", "tiktok_url", "rating",
    "review_count", "employee_count",
}


async def update_company(
    db: AsyncSession, user: User, *,
    company_id: int, fields: dict,
) -> dict:
    """Patch fields on a company. `fields` is a dict — only whitelisted
    keys are applied (everything else is silently dropped). Returns
    the updated record."""
    company = await _company_for_write(db, user, int(company_id))
    if not company:
        return {"error": "company_not_found_or_out_of_scope"}
    if not isinstance(fields, dict):
        return {"error": "fields_must_be_object"}
    applied: dict[str, Any] = {}
    for k, v in fields.items():
        if k not in _COMPANY_WRITE_FIELDS:
            continue
        if v is None:
            setattr(company, k, None)
        elif isinstance(v, str):
            setattr(company, k, v.strip()[:500])
        elif isinstance(v, (int, float)):
            setattr(company, k, v)
        else:
            continue
        applied[k] = getattr(company, k)
    if not applied:
        return {"error": "no_writable_fields_in_payload",
                "allowed_fields": sorted(_COMPANY_WRITE_FIELDS)}
    from app.services.audit_log import record_audit
    await record_audit(
        db, actor=user, action="company.updated_via_mcp",
        target_type="company", target_id=company.id, target_label=company.name,
        metadata={"applied": applied},
    )
    await db.commit()
    return {"ok": True, "company_id": company.id, "applied": applied}
WRITE_TOOL_NAMES.add("update_company")


_CONTACT_WRITE_FIELDS = {
    "first_name", "last_name", "title", "email", "phone",
    "linkedin_url", "is_primary",
}


async def update_contact(
    db: AsyncSession, user: User, *,
    contact_id: int, fields: dict,
) -> dict:
    contact = await _contact_for_write(db, user, int(contact_id))
    if not contact:
        return {"error": "contact_not_found_or_out_of_scope"}
    if not isinstance(fields, dict):
        return {"error": "fields_must_be_object"}
    applied: dict[str, Any] = {}
    for k, v in fields.items():
        if k not in _CONTACT_WRITE_FIELDS:
            continue
        if v is None:
            setattr(contact, k, None)
        elif isinstance(v, bool) and k == "is_primary":
            setattr(contact, k, bool(v))
        elif isinstance(v, str):
            setattr(contact, k, v.strip()[:500])
        else:
            continue
        applied[k] = getattr(contact, k)
    if not applied:
        return {"error": "no_writable_fields_in_payload",
                "allowed_fields": sorted(_CONTACT_WRITE_FIELDS)}
    from app.services.audit_log import record_audit
    await record_audit(
        db, actor=user, action="contact.updated_via_mcp",
        target_type="contact", target_id=contact.id, target_label=contact.full_name,
        metadata={"applied": applied},
    )
    await db.commit()
    return {"ok": True, "contact_id": contact.id, "applied": applied}
WRITE_TOOL_NAMES.add("update_contact")


async def tag_company(
    db: AsyncSession, user: User, *,
    company_id: int, tag_name: str,
) -> dict:
    """Add a tag to a company (creates the tag if it doesn't exist)."""
    from app.models import Tag
    company = await _company_for_write(db, user, int(company_id))
    if not company:
        return {"error": "company_not_found_or_out_of_scope"}
    tname = (tag_name or "").strip().lower()[:50]
    if not tname:
        return {"error": "tag_name_required"}
    tag = (await db.execute(select(Tag).where(Tag.name == tname))).scalar_one_or_none()
    if not tag:
        tag = Tag(name=tname)
        db.add(tag)
        await db.flush()
    if tag not in company.tags:
        company.tags.append(tag)
    from app.services.audit_log import record_audit
    await record_audit(
        db, actor=user, action="company.tagged_via_mcp",
        target_type="company", target_id=company.id, target_label=company.name,
        metadata={"tag": tname},
    )
    await db.commit()
    return {"ok": True, "company_id": company.id, "tag": tname}
WRITE_TOOL_NAMES.add("tag_company")


async def start_sequence(
    db: AsyncSession, user: User, *, contact_id: int,
) -> dict:
    """Generate the default sequence for a contact. Mirrors the
    UI's '▶️ Generate 30-day Sequence' button by reusing the
    existing route handler directly so the AI never produces a
    behavior that diverges from the UI."""
    contact = await _contact_for_write(db, user, int(contact_id))
    if not contact:
        return {"error": "contact_not_found_or_out_of_scope"}
    from app.routes.contact_routes import generate_contact_sequence
    try:
        result = await generate_contact_sequence(
            contact_id=int(contact_id), db=db, user=user,
        )
    except Exception as e:
        # FastAPI HTTPException carries .detail; surface gracefully
        detail = getattr(e, "detail", None) or str(e)
        return {"error": "generation_failed", "detail": str(detail)[:300]}
    from app.services.audit_log import record_audit
    await record_audit(
        db, actor=user, action="sequence.started_via_mcp",
        target_type="contact", target_id=contact.id, target_label=contact.full_name,
        metadata={"emails_created": (result or {}).get("emails_created")},
    )
    await db.commit()
    return {
        "ok": True,
        "contact_id": contact.id,
        "contact_name": contact.full_name,
        **(result or {}),
    }
WRITE_TOOL_NAMES.add("start_sequence")


async def pause_sequence(
    db: AsyncSession, user: User, *, contact_id: int,
) -> dict:
    contact = await _contact_for_write(db, user, int(contact_id))
    if not contact:
        return {"error": "contact_not_found_or_out_of_scope"}
    rows = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.contact_id == contact.id,
            GeneratedEmail.is_sent == False,
        )
    )).scalars().all()
    paused_count = 0
    for r in rows:
        if not r.paused_at:
            r.paused_at = datetime.now(timezone.utc)
            paused_count += 1
    from app.services.audit_log import record_audit
    await record_audit(
        db, actor=user, action="sequence.paused_via_mcp",
        target_type="contact", target_id=contact.id, target_label=contact.full_name,
        metadata={"steps_paused": paused_count},
    )
    await db.commit()
    return {"ok": True, "contact_id": contact.id, "steps_paused": paused_count}
WRITE_TOOL_NAMES.add("pause_sequence")


async def resume_sequence(
    db: AsyncSession, user: User, *, contact_id: int,
) -> dict:
    contact = await _contact_for_write(db, user, int(contact_id))
    if not contact:
        return {"error": "contact_not_found_or_out_of_scope"}
    rows = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.contact_id == contact.id,
            GeneratedEmail.is_sent == False,
            GeneratedEmail.paused_at.isnot(None),
        )
    )).scalars().all()
    for r in rows:
        r.paused_at = None
    from app.services.audit_log import record_audit
    await record_audit(
        db, actor=user, action="sequence.resumed_via_mcp",
        target_type="contact", target_id=contact.id, target_label=contact.full_name,
        metadata={"steps_resumed": len(rows)},
    )
    await db.commit()
    return {"ok": True, "contact_id": contact.id, "steps_resumed": len(rows)}
WRITE_TOOL_NAMES.add("resume_sequence")


async def book_meeting(
    db: AsyncSession, user: User, *,
    contact_id: int, starts_at_utc: str,
    custom_meeting_title: Optional[str] = None, note: Optional[str] = None,
) -> dict:
    """Schedule a meeting with a contact using the rep's native
    scheduler config. Validates against the rep's actual free/busy,
    creates the Google event, sends invites to both attendees, and
    logs an Activity. Same flow as the in-app 📅 Schedule modal."""
    # Reuse scheduler_routes' helper logic by calling through the
    # endpoint's underlying machinery. To keep things simple, replicate
    # the minimal verification + post — the bulk of the logic already
    # lives in the host_router endpoint; we just have to translate
    # MCP args to its request shape.
    from app.routes.scheduler_routes import (
        InternalBookingRequest, book_for_contact,
    )
    from fastapi import BackgroundTasks
    contact = await _contact_for_write(db, user, int(contact_id))
    if not contact:
        return {"error": "contact_not_found_or_out_of_scope"}
    body = InternalBookingRequest(
        contact_id=int(contact_id),
        starts_at_utc=starts_at_utc,
        custom_meeting_title=custom_meeting_title,
        note=note,
    )
    bg = BackgroundTasks()
    try:
        result = await book_for_contact(body=body, background=bg, user=user, db=db)
    except Exception as e:
        log.exception(f"book_meeting via MCP failed: {e}")
        # FastAPI's HTTPException carries .status_code + .detail
        detail = getattr(e, "detail", None) or str(e)
        return {"error": "booking_failed", "detail": str(detail)[:300]}
    # Run the queued background task synchronously (small price for
    # MCP path simplicity). The confirmation email is best-effort.
    for task in bg.tasks:
        try:
            await task()
        except Exception:
            pass
    return {"ok": True, **result}
WRITE_TOOL_NAMES.add("book_meeting")


# ============================================================
# Tool definitions for write tools (appended to existing list)
# ============================================================

TOOL_DEFINITIONS.extend([
    {
        "name": "add_note",
        "description": (
            "Log a free-form note on a company's activity timeline. "
            "Use after a call, when the rep mentions context, or to "
            "capture next steps. Permanently audited."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "company_id": {"type": "integer"},
                "content":    {"type": "string", "description": "Note text. Up to 5000 chars."},
                "contact_id": {"type": "integer", "description": "Optional — pin the note to a specific contact."},
            },
            "required": ["company_id", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_task",
        "description": (
            "Create a task on a company. Use for action items the rep "
            "needs to follow up on (e.g. 'send pricing PDF', 'check in "
            "Tuesday'). due_in_hours is relative to now."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "company_id":       {"type": "integer"},
                "description":      {"type": "string"},
                "due_in_hours":     {"type": "integer", "minimum": 1, "maximum": 8760},
                "assignee_user_id": {"type": "integer", "description": "Defaults to the calling user."},
            },
            "required": ["company_id", "description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_company",
        "description": (
            "Patch fields on a company. Provide a `fields` object with "
            "only the keys you want to change. Allowed: name, phone, "
            "address, city, state, industry, business_type, website, "
            "linkedin_url, facebook_url, instagram_url, youtube_url, "
            "tiktok_url, rating, review_count, employee_count."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "company_id": {"type": "integer"},
                "fields":     {"type": "object", "description": "Subset of writable fields keyed by name."},
            },
            "required": ["company_id", "fields"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_contact",
        "description": (
            "Patch fields on a contact. Allowed: first_name, last_name, "
            "title, email, phone, linkedin_url, is_primary."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer"},
                "fields":     {"type": "object"},
            },
            "required": ["contact_id", "fields"],
            "additionalProperties": False,
        },
    },
    {
        "name": "tag_company",
        "description": "Add a tag to a company. Tag is created if it doesn't exist.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "company_id": {"type": "integer"},
                "tag_name":   {"type": "string", "description": "Lowercased on save. Max 50 chars."},
            },
            "required": ["company_id", "tag_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "start_sequence",
        "description": (
            "Generate the default 30-day outreach sequence for a contact "
            "(13 steps: email + LinkedIn + call + iMessage). Fails if a "
            "sequence already exists — call resume_sequence in that case."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"contact_id": {"type": "integer"}},
            "required": ["contact_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pause_sequence",
        "description": "Pause all unsent steps in a contact's active sequence.",
        "inputSchema": {
            "type": "object",
            "properties": {"contact_id": {"type": "integer"}},
            "required": ["contact_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "resume_sequence",
        "description": "Un-pause previously-paused unsent steps.",
        "inputSchema": {
            "type": "object",
            "properties": {"contact_id": {"type": "integer"}},
            "required": ["contact_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "book_meeting",
        "description": (
            "Schedule a meeting with a contact using the rep's native "
            "scheduler. Validates against live free/busy, creates the "
            "Google event, sends invites to both attendees. starts_at_utc "
            "must be ISO-8601 with timezone (e.g. '2026-05-12T17:00:00Z'). "
            "Use search_companies / get_contact first to find the contact_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact_id":           {"type": "integer"},
                "starts_at_utc":        {"type": "string"},
                "custom_meeting_title": {"type": "string"},
                "note":                 {"type": "string", "description": "Goes in the calendar invite description."},
            },
            "required": ["contact_id", "starts_at_utc"],
            "additionalProperties": False,
        },
    },
])

TOOL_HANDLERS.update({
    "add_note":         add_note,
    "create_task":      create_task,
    "update_company":   update_company,
    "update_contact":   update_contact,
    "tag_company":      tag_company,
    "start_sequence":   start_sequence,
    "pause_sequence":   pause_sequence,
    "resume_sequence":  resume_sequence,
    "book_meeting":     book_meeting,
})
