"""
Sequence engine — multi-channel automated outreach.

Architecture:
  - GeneratedEmail rows are sequence steps (despite the legacy name).
  - step_type ∈ {email, imessage, call, linkedin, custom} — each has a handler.
  - auto_execute=True steps (email, imessage) fire automatically when their
    scheduled_send_at passes. auto_execute=False steps (call, linkedin) create
    a Task on the assigned BDR; sequence advances when the BDR marks the task
    complete or saves a call activity for the contact.
  - Skip conditions (skip_if_json) are checked at execution time; matching steps
    are marked skipped with an Activity log entry. Skipping does not stop the
    sequence — the next step still fires on its own schedule.
  - Listeners (in route handlers — call-connected, email-replied, iMessage-replied,
    unsubscribed, opted-out) call pause_sequence() to flip remaining steps to
    paused_at = now. Pausing stops auto-execution but preserves the rows.

This module exposes:
  - process_pending_steps(db) — engine tick. Called every ~60s by the scheduler.
  - pause_sequence(db, contact_id, reason) — used by listeners.
  - start_sequence_from_template(db, contact, template_name='30day_default')
  - DEFAULT_30DAY_TEMPLATE — the locked-in cadence Steve approved.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GeneratedEmail, Contact, Company, Activity, Task, User
from app.config import settings

logger = logging.getLogger("sequence_engine")

# Per-sender daily cap. Inbox providers throttle reputation when a single
# From-address sends > ~50 emails/day cold. Setting at 50 by default; could
# be raised after sender-domain warmup. Override via env DAILY_SEND_CAP.
import os as _os
DAILY_SEND_CAP_PER_USER = int(_os.environ.get("DAILY_SEND_CAP", "50"))


# ============================================================
# 30-day default template (Steve approved 2026-05-06)
#
# 13 touches across 30 days. iMessage doesn't appear until Day 9 — email +
# LinkedIn + Call earn the right to text first. Channel mix: 5 email, 4 call,
# 3 iMessage, 2 LinkedIn.
#
# auto_execute: True for email + imessage (engine sends). False for call + linkedin
# (engine creates a BDR Task). Skip-if conditions trigger at runtime.
# ============================================================

DEFAULT_30DAY_TEMPLATE: list[dict] = [
    {"day": 0,  "step_type": "email",     "label": "cold",            "skip_if": ["no_email", "opted_out"], "auto": True},
    {"day": 3,  "step_type": "linkedin",  "label": "linkedin_connect","skip_if": ["no_linkedin"],            "auto": False},
    {"day": 5,  "step_type": "call",      "label": "call_1",          "skip_if": ["no_phone"],               "auto": False},
    {"day": 7,  "step_type": "email",     "label": "follow_up_1",     "skip_if": ["no_email", "opted_out"], "auto": True},
    {"day": 9,  "step_type": "imessage",  "label": "imessage_1",      "skip_if": ["no_phone", "opted_out", "landline"], "auto": True},
    {"day": 12, "step_type": "call",      "label": "call_2",          "skip_if": ["no_phone"],               "auto": False},
    {"day": 15, "step_type": "email",     "label": "follow_up_2",     "skip_if": ["no_email", "opted_out"], "auto": True},
    {"day": 18, "step_type": "imessage",  "label": "imessage_2",      "skip_if": ["no_phone", "opted_out", "landline"], "auto": True},
    {"day": 20, "step_type": "linkedin",  "label": "linkedin_message","skip_if": ["no_linkedin"],            "auto": False},
    {"day": 23, "step_type": "call",      "label": "call_3",          "skip_if": ["no_phone"],               "auto": False},
    {"day": 26, "step_type": "imessage",  "label": "imessage_3",      "skip_if": ["no_phone", "opted_out", "landline"], "auto": True},
    {"day": 28, "step_type": "email",     "label": "breakup",         "skip_if": ["no_email", "opted_out"], "auto": True},
    {"day": 30, "step_type": "call",      "label": "call_final",      "skip_if": ["no_phone"],               "auto": False},
]


# ============================================================
# Skip-if evaluator
# ============================================================

def evaluate_skip(contact: Contact, conditions: list[str]) -> Optional[str]:
    """Return the first matching skip reason, or None to proceed.
    Conditions:
      - 'no_email':    contact.email is empty
      - 'no_phone':    contact.phone is empty
      - 'no_linkedin': contact.linkedin_url is empty
      - 'opted_out':   email-unsubscribed or do_not_text
      - 'landline':    phone_type == 'landline'
    """
    for cond in conditions or []:
        if cond == "no_email" and not (contact.email or "").strip():
            return "no_email"
        if cond == "no_phone" and not (contact.phone or "").strip():
            return "no_phone"
        if cond == "no_linkedin" and not (contact.linkedin_url or "").strip():
            return "no_linkedin"
        if cond == "opted_out":
            if contact.unsubscribed_at or contact.do_not_text:
                return "opted_out"
        if cond == "landline" and contact.phone_type == "landline":
            return "landline"
    return None


# ============================================================
# Step handlers
# Each handler returns (success: bool, log_message: str)
# ============================================================

async def _handle_email(db: AsyncSession, step: GeneratedEmail, contact: Contact, company: Company) -> tuple[bool, str]:
    """Send the pre-generated email through the same Resend path used for
    human-clicked sends. Activity is logged inside send_email."""
    from app.services.email_sender import send_email, get_sender_info
    from app.services.signature import render_signature

    if not settings.resend_api_key:
        return False, "Resend not configured"

    # Hard gate: verify the contact's email before we send. Hunter $0.04
    # one-time per email; cached on contact.email_status so subsequent
    # sequence steps skip the cost. Fail-open on outage.
    from app.services.email_validation import ensure_email_validated
    ok_to_send, gate_reason = await ensure_email_validated(db, contact)
    if not ok_to_send:
        return False, f"email_invalid: {gate_reason}"

    # Pick a "sender" — fall back to the company's assigned user, else the company's owner
    sender_user: Optional[User] = None
    if company.assigned_to:
        sender_user = (await db.execute(select(User).where(User.id == company.assigned_to))).scalar_one_or_none()
    if not sender_user:
        # Fallback: any admin user with sending enabled
        sender_user = (await db.execute(
            select(User).where(User.role.in_(("admin", "super_admin")), User.sending_enabled == True)
        )).scalars().first()
    if not sender_user or not sender_user.sending_enabled:
        return False, "No sending-enabled user available"

    # Daily send-cap per sender — protects deliverability. If we'd push this user
    # over the cap, defer this step to tomorrow morning instead of erroring.
    today_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = (await db.execute(
        select(func.count(GeneratedEmail.id)).where(
            GeneratedEmail.sent_by_user_id == sender_user.id,
            GeneratedEmail.is_sent == True,
            GeneratedEmail.sent_at >= today_utc,
        )
    )).scalar() or 0
    if sent_today >= DAILY_SEND_CAP_PER_USER:
        # Push to tomorrow 8am UTC (~midnight Pacific) and bail without error
        tomorrow_8am = today_utc + timedelta(days=1, hours=8)
        step.scheduled_send_at = tomorrow_8am
        logger.info(
            f"[send-cap] Deferring email step #{step.id} — sender {sender_user.email} "
            f"already sent {sent_today}/{DAILY_SEND_CAP_PER_USER} today. New schedule: {tomorrow_8am.isoformat()}"
        )
        return False, "DEFER_SEND_CAP"

    sender = get_sender_info(sender_user.first_name, sender_user.full_name)
    # Token-based Reply-To: when the prospect replies, the address routes
    # through our /api/email/inbound webhook → auto-pause + log + forward.
    # Generate the token now if missing (idempotent on re-send).
    from app.services.email_sender import generate_reply_token, reply_to_for_token
    if not step.reply_token:
        step.reply_token = generate_reply_token()
    sender["reply_to"] = reply_to_for_token(step.reply_token)
    # Wrap any URLs in the body + signature through /t/{token} for click tracking
    from app.services.tracking import wrap_html_links
    try:
        tracked_body = await wrap_html_links(
            db, step.body, contact_id=contact.id, company_id=company.id, email_id=step.id, label="body_link",
        )
    except Exception:
        tracked_body = step.body  # Fall back to untracked body
    sig_html = await render_signature(db, sender_user)
    try:
        tracked_signature = await wrap_html_links(
            db, sig_html, contact_id=contact.id, company_id=company.id, email_id=step.id, label="signature_link",
        )
    except Exception:
        tracked_signature = sig_html  # Fall back to untracked signature
    result = await send_email(
        to_email=contact.email,
        subject=step.subject,
        body=tracked_body,
        from_name=sender["from_name"],
        from_firstname=sender["from_firstname"],
        reply_to_email=sender["reply_to"],
        company_id=company.id,
        contact_id=contact.id,
        email_id=step.id,
        signature_html=tracked_signature,
        unsubscribe_token=contact.unsubscribe_token,
    )
    if not result.get("success"):
        return False, f"Resend rejected: {result.get('error', 'unknown')}"
    # Stamp the sender so future cap-checks count this email correctly
    step.sent_by_user_id = sender_user.id
    # Note: step.reply_token was set BEFORE the send (see compute_reply_to call above)
    db.add(Activity(
        company_id=company.id, contact_id=contact.id, user_id=sender_user.id,
        activity_type="email_sent",
        content=f"[Auto] Sent: {step.subject}",
    ))
    # Credit meter — shim mode (records cost, does not block)
    from app.services.credit_meter import meter, make_idem_key
    await meter(
        db,
        action_type="email_send",
        idempotency_key=make_idem_key("email_send", step.id),
        user_id=sender_user.id,
        action_ref=f"generated_email:{step.id}",
    )
    return True, "email sent"


async def _handle_imessage(db: AsyncSession, step: GeneratedEmail, contact: Contact, company: Company) -> tuple[bool, str]:
    """Auto-generate (if step.body is a placeholder) and send via Blooio."""
    from app.runtime_config import get_blooio_api_key
    from app.services.blooio_messaging import send_message as blooio_send
    from app.services.email_generator import generate_imessage

    api_key = await get_blooio_api_key(db)
    if not api_key:
        return False, "Blooio not configured"

    # If the step body is a template placeholder (or missing), generate fresh.
    # Heuristic: if body starts with "AUTO:" or is empty, regenerate using
    # current contact context. Otherwise use the stored body verbatim.
    text_body = (step.body or "").strip()
    is_placeholder = (not text_body) or text_body.upper().startswith("AUTO:")
    if is_placeholder:
        try:
            problems = json.loads(company.problems_found) if company.problems_found else []
        except (TypeError, ValueError):
            problems = []
        try:
            recent_posts = json.loads(contact.recent_posts_json) if contact.recent_posts_json else []
        except (TypeError, ValueError):
            recent_posts = []
        intent_map = {"imessage_1": "after_email", "imessage_2": "follow_up", "imessage_3": "follow_up"}
        intent = intent_map.get(step.email_type, "follow_up")
        from app.runtime_config import get_messaging_direction
        direction = await get_messaging_direction(db)
        # Audit URL for the FIRST iMessage only (don't spam the link
        # across all three).
        audit_url_for_step = None
        if step.email_type == "imessage_1":
            try:
                from app.services.audit_report import ensure_audit_for_company
                audit_url_for_step = await ensure_audit_for_company(db, company)
            except Exception:
                pass
        try:
            gen = await generate_imessage(
                business_name=company.name or "your business",
                business_type=company.business_type or company.industry or "backyard professional",
                contact_name=contact.full_name,
                problems=problems,
                recent_posts=recent_posts,
                location=(company.city or "") + ((", " + company.state) if company.state else "") or None,
                intent=intent,
                messaging_direction=direction,
                audit_url=audit_url_for_step,
            )
            text_body = gen.get("body", "").strip()
        except Exception as e:
            return False, f"AI generation failed: {e}"
        if not text_body:
            return False, "AI generation returned empty"
        # Persist the generated text on the step for audit
        step.body = text_body

    try:
        result = await blooio_send(api_key, contact.phone, text_body)
    except Exception as e:
        return False, f"Blooio error: {e}"
    if not result.success:
        return False, f"Blooio rejected: {result.error}"

    db.add(Activity(
        company_id=company.id, contact_id=contact.id,
        activity_type="imessage_sent",
        content=f"[Auto] iMessage to {contact.full_name or contact.phone}: {text_body[:200]}{'…' if len(text_body) > 200 else ''}",
        metadata_json=json.dumps({
            "channel": result.channel or "imessage",
            "message_id": result.message_id,
            "chat_id": result.chat_id,
            "to": contact.phone,
            "text": text_body,
            "sequence_step_id": step.id,
        }),
    ))
    return True, "imessage sent"


async def _handle_create_task(db: AsyncSession, step: GeneratedEmail, contact: Contact, company: Company, task_kind: str) -> tuple[bool, str]:
    """Create a Task on the assigned BDR for non-auto steps (call, linkedin).
    Sequence advances when the BDR marks the task complete (or, for call steps,
    when an activity_type='call' is logged for the contact)."""
    assignee_email = company.assigned_to
    assignee_user: Optional[User] = None
    if assignee_email:
        assignee_user = (await db.execute(select(User).where(User.email == assignee_email))).scalar_one_or_none()
    if not assignee_user:
        assignee_user = (await db.execute(select(User).where(User.role == "admin"))).scalars().first()
    if not assignee_user:
        return False, "No user to assign task to"

    # Task only has 'description' (varchar 500); pack a one-liner with the
    # contact + company. Talk track / message draft lives on the GeneratedEmail
    # row itself (step.body), accessible from the timeline.
    desc_map = {
        "call":     f"Call {contact.full_name or 'contact'} at {company.name} ({contact.phone or 'no phone'})",
        "linkedin": f"LinkedIn outreach: {contact.full_name or 'contact'} at {company.name}",
    }
    description = desc_map.get(task_kind, (step.subject or f"Sequence step for {company.name}"))[:500]

    task = Task(
        company_id=company.id,
        contact_id=contact.id,
        user_id=assignee_user.id,
        description=description,
        due_date=datetime.now(timezone.utc) + timedelta(days=1),
        completed=False,
    )
    db.add(task)
    await db.flush()  # populate task.id so we can link
    step.task_id = task.id

    db.add(Activity(
        company_id=company.id, contact_id=contact.id, user_id=assignee_user.id,
        activity_type="task_created",
        content=f"[Sequence] {description}",
    ))
    return True, f"task #{task.id} created"


# ============================================================
# TCPA send-window deferral (iMessage only — emails not regulated)
# ============================================================

def _maybe_defer_for_send_window(step: GeneratedEmail, contact: Contact, now: datetime) -> bool:
    """If we're outside the contact-local 8am-9pm window, push scheduled_send_at
    forward to the next 8am local time and return True. The engine will pick the
    step up automatically on its next tick after that deadline.

    Returns False (proceed with send) if we're inside the window or if we can't
    infer a timezone (no phone, etc.)."""
    if not contact.phone:
        return False
    from app.services.twilio_sms import check_send_window
    check = check_send_window(contact.phone, now_utc=now)
    if check.allowed:
        return False
    # Outside window — compute next 8am in the contact's local time
    local = check.contact_local_now or now
    # If we're past 9pm: next 8am is tomorrow. If we're before 8am: today's 8am.
    if local.hour >= 21:
        target_local = local.replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
    else:
        target_local = local.replace(hour=8, minute=0, second=0, microsecond=0)
    # Convert back to UTC for storage
    target_utc = target_local.astimezone(timezone.utc)
    step.scheduled_send_at = target_utc
    logger.info(
        f"[TCPA] iMessage step #{step.id} deferred to {target_utc.isoformat()} "
        f"(outside contact's 8am-9pm window: {check.reason})"
    )
    return True


# ============================================================
# Engine tick — find pending steps and execute
# ============================================================

async def process_pending_steps(db: AsyncSession, max_per_tick: int = 50) -> dict:
    """Find auto_execute steps whose scheduled_send_at has passed, evaluate
    skip-if, dispatch to the right handler. Returns counters for logging."""
    now = datetime.now(timezone.utc)
    counters = {"checked": 0, "sent": 0, "skipped": 0, "tasks_created": 0, "errors": 0}

    # Pull ready auto-execute steps (email, imessage)
    auto_rows = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.is_sent == False,
            GeneratedEmail.paused_at.is_(None),
            GeneratedEmail.skipped_at.is_(None),
            GeneratedEmail.auto_execute == True,
            GeneratedEmail.scheduled_send_at != None,
            GeneratedEmail.scheduled_send_at <= now,
        ).order_by(GeneratedEmail.scheduled_send_at).limit(max_per_tick)
    )).scalars().all()

    # Also pull non-auto steps that need a Task created (call, linkedin)
    task_rows = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.is_sent == False,
            GeneratedEmail.paused_at.is_(None),
            GeneratedEmail.skipped_at.is_(None),
            GeneratedEmail.auto_execute == False,
            GeneratedEmail.task_id.is_(None),  # not yet materialized as a Task
            GeneratedEmail.scheduled_send_at != None,
            GeneratedEmail.scheduled_send_at <= now,
        ).order_by(GeneratedEmail.scheduled_send_at).limit(max_per_tick)
    )).scalars().all()

    for step in list(auto_rows) + list(task_rows):
        counters["checked"] += 1
        contact = (await db.execute(select(Contact).where(Contact.id == step.contact_id))).scalar_one_or_none()
        company = (await db.execute(select(Company).where(Company.id == step.company_id))).scalar_one_or_none()
        if not contact or not company:
            step.skipped_at = now
            step.skip_reason = "missing_contact_or_company"
            counters["skipped"] += 1
            continue

        # Skip-if check
        skip_conds = []
        try:
            skip_conds = json.loads(step.skip_if_json) if step.skip_if_json else []
        except (TypeError, ValueError):
            skip_conds = []
        skip_reason = evaluate_skip(contact, skip_conds)
        if skip_reason:
            step.skipped_at = now
            step.skip_reason = skip_reason
            db.add(Activity(
                company_id=company.id, contact_id=contact.id,
                activity_type="sequence_step_skipped",
                content=f"[Auto] Skipped {step.step_type} step #{step.sequence_order} — reason: {skip_reason}",
            ))
            counters["skipped"] += 1
            continue

        # Dispatch
        try:
            if step.step_type == "email":
                ok, msg = await _handle_email(db, step, contact, company)
                if ok:
                    step.is_sent = True
                    step.sent_at = now
                    counters["sent"] += 1
                elif msg == "DEFER_SEND_CAP":
                    # Cap hit — step.scheduled_send_at was bumped inside the handler
                    counters.setdefault("deferred", 0)
                    counters["deferred"] += 1
                else:
                    counters["errors"] += 1
                    logger.warning(f"Email step #{step.id} failed: {msg}")
            elif step.step_type == "imessage":
                # TCPA: don't fire iMessages outside 8am-9pm contact-local time.
                # Defer to next 8am local instead of skipping or erroring.
                deferred = _maybe_defer_for_send_window(step, contact, now)
                if deferred:
                    counters.setdefault("deferred", 0)
                    counters["deferred"] += 1
                else:
                    ok, msg = await _handle_imessage(db, step, contact, company)
                    if ok:
                        step.is_sent = True
                        step.sent_at = now
                        counters["sent"] += 1
                    else:
                        counters["errors"] += 1
                        logger.warning(f"iMessage step #{step.id} failed: {msg}")
            elif step.step_type in ("call", "linkedin"):
                ok, msg = await _handle_create_task(db, step, contact, company, step.step_type)
                if ok:
                    counters["tasks_created"] += 1
                else:
                    counters["errors"] += 1
                    logger.warning(f"Task creation for step #{step.id} failed: {msg}")
            else:
                logger.info(f"Unknown step_type '{step.step_type}' on step #{step.id} — leaving in place")
        except Exception as e:
            logger.exception(f"Unhandled error processing step #{step.id}: {e}")
            counters["errors"] += 1

    await db.commit()
    return counters


# ============================================================
# Pause / resume / start
# ============================================================

async def pause_sequence(db: AsyncSession, contact_id: int, reason: str, sequence_label: str = "main") -> int:
    """Pause all not-yet-sent steps for a contact. Used by listeners (reply,
    call-connected, opt-out). Returns number of steps paused."""
    now = datetime.now(timezone.utc)
    rows = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.contact_id == contact_id,
            GeneratedEmail.sequence_label == sequence_label,
            GeneratedEmail.is_sent == False,
            GeneratedEmail.paused_at.is_(None),
            GeneratedEmail.skipped_at.is_(None),
        )
    )).scalars().all()
    for r in rows:
        r.paused_at = now
    if rows:
        first = rows[0]
        db.add(Activity(
            company_id=first.company_id, contact_id=contact_id,
            activity_type="sequence_paused",
            content=f"[Auto] Sequence paused — reason: {reason} ({len(rows)} steps remaining)",
        ))
    return len(rows)


async def resume_sequence(db: AsyncSession, contact_id: int, sequence_label: str = "main") -> int:
    """Un-pause and re-anchor scheduling so the sequence picks up from where
    it left off (rather than firing all paused steps at once)."""
    now = datetime.now(timezone.utc)
    rows = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.contact_id == contact_id,
            GeneratedEmail.sequence_label == sequence_label,
            GeneratedEmail.is_sent == False,
            GeneratedEmail.paused_at != None,
            GeneratedEmail.skipped_at.is_(None),
        ).order_by(GeneratedEmail.sequence_order)
    )).scalars().all()
    if not rows:
        return 0
    # Find the earliest scheduled step's original delay, anchor to "now"
    base_day = rows[0].send_delay_days or 0
    base_time = now
    for r in rows:
        offset_days = (r.send_delay_days or 0) - base_day
        r.scheduled_send_at = base_time + timedelta(days=max(offset_days, 0))
        r.paused_at = None
    db.add(Activity(
        company_id=rows[0].company_id, contact_id=contact_id,
        activity_type="sequence_resumed",
        content=f"[Auto] Sequence resumed — {len(rows)} steps re-scheduled from now",
    ))
    return len(rows)


async def start_sequence_from_template(
    db: AsyncSession,
    contact: Contact,
    template: list[dict] = None,
    sequence_label: str = "main",
    pre_generate_emails: bool = True,
) -> int:
    """Materialize a template into GeneratedEmail rows for the contact.
    Returns the number of steps created. Skips contact entirely if they're
    already opted out / unsubscribed.

    Email steps are pre-generated using the existing email_generator (so the
    subject/body is real text, not placeholder). iMessage step bodies are
    left as 'AUTO:' so the engine generates fresh at send time using the
    most-recent contact context.
    """
    if template is None:
        template = DEFAULT_30DAY_TEMPLATE

    if contact.unsubscribed_at:
        return 0

    company = (await db.execute(select(Company).where(Company.id == contact.company_id))).scalar_one_or_none()
    if not company:
        return 0

    now = datetime.now(timezone.utc)

    # Load org-wide messaging direction once and thread it through every Claude
    # call — keeps the strategic angle (AI findability / GEO / local SEO by
    # default) consistent across email, iMessage, and post-call follow-ups.
    from app.runtime_config import get_messaging_direction
    direction = await get_messaging_direction(db)

    # Build problems + recent posts context once (used by both email + imessage gen)
    try:
        problems = json.loads(company.problems_found) if company.problems_found else []
    except (TypeError, ValueError):
        problems = []
    try:
        recent_posts = json.loads(contact.recent_posts_json) if contact.recent_posts_json else []
    except (TypeError, ValueError):
        recent_posts = []

    # Pre-generate email content for email steps so previews work immediately
    email_drafts: dict[str, dict] = {}
    # Get-or-create the AI Findability audit so follow-up emails +
    # iMessage steps can naturally share the link. None on any failure
    # — sequence still generates, just without the link.
    audit_url = None
    try:
        from app.services.audit_report import ensure_audit_for_company
        audit_url = await ensure_audit_for_company(db, company)
    except Exception as e:
        logger.warning(f"Audit pre-generation failed for company {company.id}: {e}")

    if pre_generate_emails and contact.email:
        try:
            from app.services.email_generator import generate_cold_email, generate_follow_up
            for tstep in template:
                if tstep["step_type"] != "email":
                    continue
                if tstep["label"] == "cold":
                    draft = await generate_cold_email(
                        business_name=company.name,
                        business_type=company.business_type or company.industry or "backyard professional",
                        website=company.website or "",
                        problems=problems,
                        contact_name=contact.full_name,
                        location=company.city,
                        messaging_direction=direction,
                    )
                else:
                    # follow_up_1 → #1, follow_up_2 → #2, breakup → #3
                    fu_num_map = {"follow_up_1": 1, "follow_up_2": 2, "breakup": 3}
                    fu_num = fu_num_map.get(tstep["label"], 1)
                    cold_subject = email_drafts.get("cold", {}).get("subject", "")
                    draft = await generate_follow_up(
                        business_name=company.name,
                        business_type=company.business_type or company.industry or "backyard professional",
                        problems=problems,
                        previous_email_subject=cold_subject,
                        follow_up_number=fu_num,
                        contact_name=contact.full_name,
                        messaging_direction=direction,
                        audit_url=audit_url,
                    )
                email_drafts[tstep["label"]] = draft
        except Exception as e:
            logger.warning(f"Email pre-generation failed for contact {contact.id}: {e}")

    # Pre-generate iMessage bodies too — same reason as emails: BDR can preview
    # and edit the actual text before it fires. Each label gets a different
    # intent so the 3 iMessages don't read identically.
    imessage_drafts: dict[str, dict] = {}
    if contact.phone:
        try:
            from app.services.email_generator import generate_imessage
            intent_map = {"imessage_1": "after_email", "imessage_2": "follow_up", "imessage_3": "follow_up"}
            for tstep in template:
                if tstep["step_type"] != "imessage":
                    continue
                # Only the first iMessage step drops the audit URL — by
                # the time imessage_2 / imessage_3 fire the prospect has
                # already seen it. Reduces link spam.
                msg_audit_url = audit_url if tstep["label"] == "imessage_1" else None
                draft = await generate_imessage(
                    business_name=company.name or "your business",
                    business_type=company.business_type or company.industry or "backyard professional",
                    contact_name=contact.full_name,
                    problems=problems,
                    recent_posts=recent_posts,
                    location=(company.city or "") + ((", " + company.state) if company.state else "") or None,
                    intent=intent_map.get(tstep["label"], "follow_up"),
                    messaging_direction=direction,
                    audit_url=msg_audit_url,
                )
                imessage_drafts[tstep["label"]] = draft
        except Exception as e:
            logger.warning(f"iMessage pre-generation failed for contact {contact.id}: {e}")

    created = 0
    for idx, tstep in enumerate(template, start=1):
        body = "AUTO:"
        subject = ""
        if tstep["step_type"] == "email":
            d = email_drafts.get(tstep["label"], {})
            subject = d.get("subject", f"Step {idx}")
            body = d.get("body", "AUTO:")
        elif tstep["step_type"] == "imessage":
            subject = f"iMessage step {idx}"
            d = imessage_drafts.get(tstep["label"], {})
            body = d.get("body") or "AUTO:"  # falls back to send-time generation if pre-gen failed
        elif tstep["step_type"] == "call":
            subject = f"Call {idx}"
            body = (
                f"Call talk track:\n\n"
                f"- Hi {contact.first_name or 'there'} — Steve from Backyard Marketing Pros.\n"
                f"- I sent you a note about {company.name} earlier; wanted to catch you live.\n"
                f"- Quick reason for the call: [reference a specific problem from the audit].\n"
                f"- Got 5 min later this week to dig in?\n\n"
                f"If voicemail: short message + send a follow-up email/text the same day."
            )
        elif tstep["step_type"] == "linkedin":
            subject = f"LinkedIn step {idx}"
            body = (
                f"Connect note (under 280 chars):\n\n"
                f"Hey {contact.first_name or 'there'} — saw your work at {company.name}. "
                f"Love connecting with fellow backyard pros.\n\n"
                f"(After accept) DM with one specific insight from their site/Google reviews."
            )

        step = GeneratedEmail(
            contact_id=contact.id,
            company_id=company.id,
            step_type=tstep["step_type"],
            email_type=tstep["label"],
            subject=subject,
            body=body,
            sequence_order=idx,
            send_delay_days=tstep["day"],
            scheduled_send_at=now + timedelta(days=tstep["day"]),
            skip_if_json=json.dumps(tstep.get("skip_if", [])),
            auto_execute=bool(tstep.get("auto", False)),
            sequence_label=sequence_label,
            payload_json=None,
        )
        db.add(step)
        created += 1

    if created > 0:
        company.status = "sequencing"
        if hasattr(company, "sequence_started_at"):
            company.sequence_started_at = now
        db.add(Activity(
            company_id=company.id, contact_id=contact.id,
            activity_type="sequence_created",
            content=f"[30-day] Sequence started — {created} steps queued",
        ))
    await db.commit()
    return created
