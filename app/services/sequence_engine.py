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

from app.models import GeneratedEmail, Contact, Company, Activity, Task, User, SequenceTemplate
from app.config import settings

logger = logging.getLogger("sequence_engine")

# Per-sender daily cap. Inbox providers throttle reputation when a single
# From-address sends > ~50 emails/day cold. Setting at 50 by default; could
# be raised after sender-domain warmup. Override via env DAILY_SEND_CAP.
import os as _os
DAILY_SEND_CAP_PER_USER = int(_os.environ.get("DAILY_SEND_CAP", "50"))

# How long to wait on a manual step (LinkedIn / call / iMessage with
# auto_execute=False) before auto-skipping it. Auto-emails further down
# the sequence already fire on their own scheduled_send_at — they never
# wait on a manual upstream step — but unsent manual rows pile up in the
# Stalled tab and make sequences look broken. After this many days past
# scheduled_send_at, auto-skip the step with skip_reason="manual_overdue_<N>d"
# so the sequence keeps a clean shape. Override via env MANUAL_AUTOSKIP_DAYS.
MANUAL_AUTOSKIP_DAYS = int(_os.environ.get("MANUAL_AUTOSKIP_DAYS", "3"))


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
    {"day": 5,  "step_type": "call",      "label": "call_1",          "skip_if": [],                         "auto": False},
    {"day": 7,  "step_type": "email",     "label": "follow_up_1",     "skip_if": ["no_email", "opted_out"], "auto": True},
    {"day": 9,  "step_type": "imessage",  "label": "imessage_1",      "skip_if": ["no_phone", "opted_out", "landline"], "auto": True},
    {"day": 12, "step_type": "call",      "label": "call_2",          "skip_if": [],                         "auto": False},
    {"day": 15, "step_type": "email",     "label": "follow_up_2",     "skip_if": ["no_email", "opted_out"], "auto": True},
    {"day": 18, "step_type": "imessage",  "label": "imessage_2",      "skip_if": ["no_phone", "opted_out", "landline"], "auto": True},
    {"day": 20, "step_type": "linkedin",  "label": "linkedin_message","skip_if": ["no_linkedin"],            "auto": False},
    {"day": 23, "step_type": "call",      "label": "call_3",          "skip_if": [],                         "auto": False},
    {"day": 26, "step_type": "imessage",  "label": "imessage_3",      "skip_if": ["no_phone", "opted_out", "landline"], "auto": True},
    {"day": 28, "step_type": "email",     "label": "breakup",         "skip_if": ["no_email", "opted_out"], "auto": True},
    {"day": 30, "step_type": "call",      "label": "call_final",      "skip_if": [],                         "auto": False},
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
        return False, "Email service not configured"

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
        step_type=step.step_type,  # defense in depth — send_email refuses if not 'email'
    )
    if not result.get("success"):
        # Transient Resend failure (timeout, 5xx, network blip) — return
        # a TRANSIENT: prefix the dispatch loop can detect, so the step
        # stays pending and the next engine tick re-attempts. Without
        # this, every Resend hiccup burned an error counter + the step
        # would sit "errored" forever (Sentry 970c574, 2026-06-03).
        if result.get("retryable"):
            return False, f"TRANSIENT:{result.get('error', 'transient')}"
        return False, f"Email send failed: {result.get('error', 'unknown')}"
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
    """Auto-generate (if step.body is a placeholder) and send via Blooio.

    Returns (ok, msg). The CALLER is responsible for marking the step
    skipped vs. retried based on the failure mode — see the dispatch
    loop in process_pending_steps. The 'skip' return-value convention:
    msg='SKIP:<reason>' tells the caller to set skipped_at on the step
    so the engine never re-attempts it (vs. plain "failed: …" which
    lets the engine try again next tick).
    """
    from app.runtime_config import _get_or_create
    from app.services.blooio_messaging import send_message as blooio_send
    from app.services.email_generator import generate_imessage

    # Tenant toggle check first — if iMessage is paused for this tenant,
    # we skip immediately. Avoids the 'no active devices' thrash we hit
    # when Blooio is configured but the device link is unhealthy.
    rc = await _get_or_create(db)
    if not getattr(rc, "imessage_enabled", False):
        return False, "SKIP:imessage_disabled_by_tenant"

    api_key = (getattr(rc, "blooio_api_key", None) or "").strip()
    if not api_key:
        return False, "SKIP:imessage_not_configured"

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
        return False, f"iMessage error: {e}"
    if not result.success:
        return False, f"iMessage delivery failed: {result.error}"

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
    assignee_user: Optional[User] = None
    if company.assigned_to:
        assignee_user = (await db.execute(select(User).where(User.id == company.assigned_to))).scalar_one_or_none()
    if not assignee_user:
        assignee_user = (await db.execute(
            select(User).where(User.role.in_(("admin", "super_admin")))
        )).scalars().first()
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
# Send-window deferral — applies to every auto-channel.
# ============================================================
#
# The window is configured org-wide in runtime_config (defaults 8am-7pm
# contact-local, every day). iMessage/SMS additionally clamp to TCPA's
# 8am-9pm legal limit even if admin widened the org window.
#
# When a step is outside the window, we don't skip or error — we push
# scheduled_send_at forward to the next valid window-start. The engine
# picks it up automatically on a later tick.

async def _maybe_defer_for_send_window(
    db: AsyncSession,
    step: GeneratedEmail,
    contact: Contact,
    company: Optional[Company],
    rep: Optional[User],
    now: datetime,
    *,
    channel: str,
) -> bool:
    """Returns True (and bumps scheduled_send_at) if we're outside the
    configured send window. Returns False (proceed) if we're inside."""
    from app.services import send_window as _sw
    allowed, reason = await _sw.is_now_sendable(
        db, contact=contact, company=company, rep=rep, channel=channel, now_utc=now,
    )
    if allowed:
        return False
    cfg = await _sw.get_autopilot_config(db)
    contact_tz = _sw.infer_contact_timezone(contact, company, rep)
    rep_tz = _sw.infer_rep_timezone(rep)
    target_utc = _sw.next_window_start(
        after_utc=now, contact_tz=contact_tz, rep_tz=rep_tz,
        cfg=cfg, channel=channel,
    )
    step.scheduled_send_at = target_utc
    logger.info(
        f"[SEND-WINDOW] {channel} step #{step.id} deferred to {target_utc.isoformat()} — {reason}"
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

    # ---- Wake snoozed companies whose resume_at has passed ----
    # When a BDR snoozed a company, sequence_resume_at was set + the
    # NOT IN gate below suppressed dispatch. Now that the timestamp has
    # passed, regenerate a fresh tailored sequence anchored at today and
    # clear the snooze fields. The first email of the new sequence
    # references the agreed timeframe ("you asked me to follow back up
    # in N days — circling back as promised").
    # Cap WAY down (was 20) because wake_sequence_for_company runs Claude
    # email-body generation INLINE per contact — blocking the dispatch
    # loop for tens of seconds per company at scale. Code-review #2.
    # Lift back up when wake is moved to a background queue.
    waking_companies = (await db.execute(
        select(Company).where(
            Company.sequence_resume_at.is_not(None),
            Company.sequence_resume_at <= now,
        ).order_by(Company.sequence_resume_at).limit(3)
    )).scalars().all()
    for company in waking_companies:
        try:
            woken = await wake_sequence_for_company(db, company)
            counters.setdefault("woken", 0)
            counters["woken"] += woken
        except Exception as e:
            logger.exception(f"Wake failed for company #{company.id}: {e}")

    # ---- Auto-resume paused sequences after quiet period ----
    # If a contact's sequence was paused (e.g. they replied, then went
    # cold), the template can request that the sequence resume after
    # auto_resume_days of no activity. We resume by clearing paused_at
    # on every remaining step for that contact AND bumping their
    # scheduled_send_at forward so steps don't all fire at once.
    #
    # Activity definition: any Activity row on the contact since the
    # pause (reply, call, manual touch). If nothing new since pause,
    # the contact has gone quiet — safe to start the cadence again.
    default_tmpl_row = (await db.execute(
        select(SequenceTemplate).where(
            SequenceTemplate.is_default == True,
            SequenceTemplate.is_active == True,
        ).limit(1)
    )).scalar_one_or_none()
    auto_resume_days = (default_tmpl_row.auto_resume_days if default_tmpl_row else 0) or 0
    if auto_resume_days > 0:
        resume_cutoff = now - timedelta(days=auto_resume_days)
        paused_contacts = (await db.execute(
            select(GeneratedEmail.contact_id, func.max(GeneratedEmail.paused_at).label("paused_at"))
            .where(
                GeneratedEmail.paused_at.is_not(None),
                GeneratedEmail.is_sent == False,
                GeneratedEmail.skipped_at.is_(None),
            )
            .group_by(GeneratedEmail.contact_id)
            .having(func.max(GeneratedEmail.paused_at) < resume_cutoff)
            .limit(max_per_tick)
        )).all()
        for contact_id, paused_at in paused_contacts:
            recent = (await db.execute(
                select(Activity.id).where(
                    Activity.contact_id == contact_id,
                    Activity.created_at > paused_at,
                    Activity.activity_type.in_((
                        "email_replied", "imessage_received", "call",
                        "email_opened", "email_clicked", "note",
                    )),
                ).limit(1)
            )).scalar_one_or_none()
            if recent is not None:
                continue  # contact engaged since the pause — keep sequence paused
            # Resume: clear paused_at on remaining steps + re-anchor schedules
            # so they fire on a fresh cadence (first step today, subsequent
            # steps spaced by original deltas).
            remaining = (await db.execute(
                select(GeneratedEmail).where(
                    GeneratedEmail.contact_id == contact_id,
                    GeneratedEmail.paused_at.is_not(None),
                    GeneratedEmail.is_sent == False,
                    GeneratedEmail.skipped_at.is_(None),
                ).order_by(GeneratedEmail.sequence_order)
            )).scalars().all()
            if not remaining:
                continue
            anchor_old = remaining[0].scheduled_send_at or now
            for step in remaining:
                step.paused_at = None
                if step.scheduled_send_at:
                    delta = step.scheduled_send_at - anchor_old
                    step.scheduled_send_at = now + delta
            company_id = remaining[0].company_id
            db.add(Activity(
                company_id=company_id, contact_id=contact_id,
                activity_type="sequence_resumed",
                content=(
                    f"[Auto] Sequence auto-resumed — paused {auto_resume_days}+ days "
                    f"with no further activity. {len(remaining)} steps re-anchored."
                ),
            ))

    # ---- Auto-skip overdue manual steps ----
    # LinkedIn / call / iMessage steps with auto_execute=False rely on a BDR
    # to act. If they've been overdue >MANUAL_AUTOSKIP_DAYS without action,
    # skip them so the sequence keeps a clean shape. Downstream auto-emails
    # are unaffected because they fire on their own scheduled_send_at — the
    # engine does not enforce upstream-step dependencies. We respect linked
    # Task completion: if the Task is done, complete_task should have already
    # marked the step sent — we leave any such row alone.
    autoskip_cutoff = now - timedelta(days=MANUAL_AUTOSKIP_DAYS)
    overdue_manual = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.is_sent == False,
            GeneratedEmail.paused_at.is_(None),
            GeneratedEmail.skipped_at.is_(None),
            GeneratedEmail.auto_execute == False,
            GeneratedEmail.step_type.in_(("linkedin", "call", "imessage")),
            GeneratedEmail.scheduled_send_at != None,
            GeneratedEmail.scheduled_send_at < autoskip_cutoff,
        ).limit(max_per_tick)
    )).scalars().all()
    for step in overdue_manual:
        if step.task_id:
            t = (await db.execute(select(Task).where(Task.id == step.task_id))).scalar_one_or_none()
            if t and t.completed:
                continue  # complete_task should have handled it — leave alone
        step.skipped_at = now
        step.skip_reason = f"manual_overdue_{MANUAL_AUTOSKIP_DAYS}d"
        db.add(Activity(
            company_id=step.company_id, contact_id=step.contact_id,
            activity_type="sequence_step_skipped",
            content=(
                f"[Auto] Skipped {step.step_type} step #{step.sequence_order} — "
                f"manual action overdue >{MANUAL_AUTOSKIP_DAYS}d. Downstream auto-steps continue."
            ),
        ))
        counters["skipped"] += 1

    # Company-snooze gate: a step is suppressed while its company is
    # snoozed (sequence_resume_at > now). On wake, the engine regenerates
    # a fresh sequence for the company so we don't need to mutate these
    # rows now — just don't dispatch them. Bind the snoozed-company ids
    # ONCE per tick so both queries reuse a small in-memory NOT IN list
    # (cheaper than a NOT EXISTS subquery against a tiny set).
    snoozed_company_ids = (await db.execute(
        select(Company.id).where(
            Company.sequence_resume_at.is_not(None),
            Company.sequence_resume_at > now,
        )
    )).scalars().all()

    # Pull ready auto-execute steps (email, imessage)
    auto_q = select(GeneratedEmail).where(
        GeneratedEmail.is_sent == False,
        GeneratedEmail.paused_at.is_(None),
        GeneratedEmail.skipped_at.is_(None),
        GeneratedEmail.auto_execute == True,
        GeneratedEmail.scheduled_send_at != None,
        GeneratedEmail.scheduled_send_at <= now,
    )
    if snoozed_company_ids:
        auto_q = auto_q.where(GeneratedEmail.company_id.notin_(snoozed_company_ids))
    auto_rows = (await db.execute(
        auto_q.order_by(GeneratedEmail.scheduled_send_at).limit(max_per_tick)
    )).scalars().all()

    # Also pull non-auto steps that need a Task created (call, linkedin)
    task_q = select(GeneratedEmail).where(
        GeneratedEmail.is_sent == False,
        GeneratedEmail.paused_at.is_(None),
        GeneratedEmail.skipped_at.is_(None),
        GeneratedEmail.auto_execute == False,
        GeneratedEmail.task_id.is_(None),  # not yet materialized as a Task
        GeneratedEmail.scheduled_send_at != None,
        GeneratedEmail.scheduled_send_at <= now,
    )
    if snoozed_company_ids:
        task_q = task_q.where(GeneratedEmail.company_id.notin_(snoozed_company_ids))
    task_rows = (await db.execute(
        task_q.order_by(GeneratedEmail.scheduled_send_at).limit(max_per_tick)
    )).scalars().all()

    for step in list(auto_rows) + list(task_rows):
        counters["checked"] += 1
        contact = (await db.execute(select(Contact).where(Contact.id == step.contact_id))).scalar_one_or_none()
        company = (await db.execute(select(Company).where(Company.id == step.company_id))).scalar_one_or_none()
        # Owner of the company → used as send-window timezone fallback
        # when neither the phone area code nor the company state tell us.
        rep = None
        if company and company.assigned_to:
            rep = (await db.execute(select(User).where(User.id == company.assigned_to))).scalar_one_or_none()
        if not contact or not company:
            step.skipped_at = now
            step.skip_reason = "missing_contact_or_company"
            counters["skipped"] += 1
            continue

        # Phase 7 cutover gate: when contacts.outreach_owner != 'legacy',
        # the new engagement engine owns this contact ENTIRELY — all
        # channels, all steps. The new engine's CallTaskChannel and
        # ManualChannel write rows to the existing `tasks` table (linked
        # via tasks.engagement_action_id), so BDR-handled steps still
        # surface in the existing CRM task view exactly like before.
        # Single engine, no split, no slop.
        owner = getattr(contact, "outreach_owner", None) or "legacy"
        if owner != "legacy":
            step.skipped_at = now
            step.skip_reason = f"outreach_owner={owner}"
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
                # Org send window applies to email too (not regulated by
                # TCPA, but we still don't want 11pm sends).
                deferred = await _maybe_defer_for_send_window(
                    db, step, contact, company, rep, now, channel="email"
                )
                if deferred:
                    counters.setdefault("deferred", 0)
                    counters["deferred"] += 1
                else:
                    ok, msg = await _handle_email(db, step, contact, company)
                    if ok:
                        step.is_sent = True
                        step.sent_at = now
                        counters["sent"] += 1
                    elif msg == "DEFER_SEND_CAP":
                        counters.setdefault("deferred", 0)
                        counters["deferred"] += 1
                    elif msg.startswith("TRANSIENT:"):
                        # Resend timeout / 5xx / network blip. Leave the
                        # step pending so the next engine tick retries.
                        # Don't burn the error counter — this isn't an
                        # error in OUR system, just upstream slowness.
                        counters.setdefault("transient_retry", 0)
                        counters["transient_retry"] += 1
                        logger.info(f"Email step #{step.id} transient — will retry next tick: {msg[10:]}")
                    else:
                        counters["errors"] += 1
                        logger.warning(f"Email step #{step.id} failed: {msg}")
            elif step.step_type == "imessage":
                # Same window as email, but iMessage is also clamped to
                # TCPA's 8am-9pm contact-local even if admin widened the
                # org window. The service handles that internally.
                deferred = await _maybe_defer_for_send_window(
                    db, step, contact, company, rep, now, channel="imessage"
                )
                if deferred:
                    counters.setdefault("deferred", 0)
                    counters["deferred"] += 1
                else:
                    ok, msg = await _handle_imessage(db, step, contact, company)
                    if ok:
                        step.is_sent = True
                        step.sent_at = now
                        counters["sent"] += 1
                    elif msg.startswith("SKIP:"):
                        # iMessage is paused or unconfigured for this tenant.
                        # Mark the step skipped so the engine moves on +
                        # the next step in the sequence isn't blocked.
                        reason = msg.split(":", 1)[1] or "imessage_unavailable"
                        step.skipped_at = now
                        step.skip_reason = reason
                        db.add(Activity(
                            company_id=company.id, contact_id=contact.id,
                            activity_type="sequence_step_skipped",
                            content=f"[Auto] Skipped iMessage step #{step.sequence_order} — reason: {reason}",
                        ))
                        counters["skipped"] += 1
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
# Manual single-step execution (sidebar / chrome ext / admin trigger)
# ============================================================

async def execute_step_now(
    db: AsyncSession,
    step_id: int,
    triggered_by_user_id: Optional[int] = None,
) -> dict:
    """Fire ONE specific step right now, bypassing the scheduled_send_at
    gate. Used by the Missive sidebar 'Send next step now' button and
    will back the Chrome extension's send action.

    Still respects everything else the engine cares about: skip-if
    rules, send-window guards for iMessage, the per-sender send cap.
    Returns {fired, step_id, step_type, reason, error?}."""
    now = datetime.now(timezone.utc)
    step = (await db.execute(
        select(GeneratedEmail).where(GeneratedEmail.id == step_id)
    )).scalar_one_or_none()
    if not step:
        return {"fired": False, "reason": "step_not_found"}
    if step.is_sent or step.paused_at or step.skipped_at:
        return {"fired": False, "reason": f"step_unavailable (sent={bool(step.is_sent)} paused={bool(step.paused_at)} skipped={bool(step.skipped_at)})"}

    contact = (await db.execute(select(Contact).where(Contact.id == step.contact_id))).scalar_one_or_none()
    company = (await db.execute(select(Company).where(Company.id == step.company_id))).scalar_one_or_none()
    if not contact or not company:
        return {"fired": False, "reason": "missing_contact_or_company"}

    # Phase 7 cutover gate — same as auto-run: the new engine owns
    # everything when outreach_owner != 'legacy'. BDR-handled actions
    # surface via the existing CRM task view through the new engine's
    # tasks.engagement_action_id linkage.
    owner = getattr(contact, "outreach_owner", None) or "legacy"
    if owner != "legacy":
        return {"fired": False, "reason": f"outreach_owner={owner}"}

    # Skip-if check — same as auto-run path
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
            user_id=triggered_by_user_id,
            activity_type="sequence_step_skipped",
            content=f"[Manual] Skipped {step.step_type} step #{step.sequence_order} — reason: {skip_reason}",
        ))
        await db.commit()
        return {"fired": False, "reason": f"skip_if: {skip_reason}"}

    # Dispatch by type
    try:
        if step.step_type == "email":
            ok, msg = await _handle_email(db, step, contact, company)
            if ok:
                step.is_sent = True
                step.sent_at = now
                db.add(Activity(
                    company_id=company.id, contact_id=contact.id,
                    user_id=triggered_by_user_id,
                    activity_type="sequence_step_manual_send",
                    content=f"[Manual] Sent {step.step_type} step #{step.sequence_order}: {step.subject}",
                ))
                await db.commit()
                return {"fired": True, "step_id": step.id, "step_type": step.step_type}
            return {"fired": False, "reason": msg}
        elif step.step_type == "imessage":
            ok, msg = await _handle_imessage(db, step, contact, company)
            if ok:
                step.is_sent = True
                step.sent_at = now
                await db.commit()
                return {"fired": True, "step_id": step.id, "step_type": step.step_type}
            return {"fired": False, "reason": msg}
        elif step.step_type in ("call", "linkedin"):
            ok, msg = await _handle_create_task(db, step, contact, company, step.step_type)
            if ok:
                await db.commit()
                return {"fired": True, "step_id": step.id, "step_type": step.step_type, "result": "task_created"}
            return {"fired": False, "reason": msg}
        else:
            return {"fired": False, "reason": f"unknown_step_type:{step.step_type}"}
    except Exception as e:
        logger.exception(f"execute_step_now({step_id}) failed: {e}")
        return {"fired": False, "reason": "exception", "error": str(e)}


# ============================================================
# Pause / resume / start
# ============================================================

def _re_engagement_first_step(contact: Contact, company: Company, snooze_days: Optional[int], snooze_reason: Optional[str]) -> dict:
    """Build the day-0 'circling back' email that replaces the cold-intro
    when a snoozed company wakes up. Deterministic copy (no AI call) — the
    pattern is too predictable to justify burning credits on every wake."""
    first_name = (contact.full_name or "").strip().split(" ")[0] if contact.full_name else ""
    greeting = f"Hi {first_name}," if first_name else "Hi,"
    timeframe_line = (
        f"You asked me to circle back in {snooze_days} days — checking in as promised."
        if snooze_days
        else "Circling back as promised."
    )
    company_label = (company.name or "your company").strip()
    body = (
        f"{greeting}\n\n"
        f"{timeframe_line}\n\n"
        f"Has anything changed at {company_label} since we last spoke? "
        "Worth a quick 15-minute chat this week to see if it makes sense to revisit?\n\n"
        "Either way, appreciate you being straight with me before — happy to disappear "
        "again if the timing still isn't right."
    )
    return {
        "step_type": "email",
        "subject": "circling back as promised",
        "body": body,
        "label": "re_engagement",
        "day": 0,
    }


async def wake_sequence_for_company(db: AsyncSession, company: Company) -> int:
    """Wake a snoozed company: regenerate a fresh tailored sequence
    anchored at now, with the first email referencing the snooze
    timeframe. Returns the number of new steps materialized.

    Side effects:
      - Marks all unsent + non-skipped steps for the company's contacts
        as skipped with reason 'regenerated_post_snooze' (so they don't
        fire after the gate clears)
      - Clears company.sequence_resume_at + related fields
      - Writes Activity 'sequence_woke_from_snooze'

    Idempotent enough to call from both the engine tick + the manual
    /unsnooze route. Safe if the company isn't actually snoozed (no-op).
    """
    now = datetime.now(timezone.utc)
    if company.sequence_resume_at is None and company.sequence_snoozed_at is None:
        return 0  # not snoozed — nothing to wake

    snooze_days = company.sequence_snooze_days
    snooze_reason = company.sequence_snooze_reason

    # 1) Mark every remaining unsent step across all contacts as skipped.
    contacts = (await db.execute(
        select(Contact).where(Contact.company_id == company.id)
    )).scalars().all()
    contact_ids = [c.id for c in contacts]
    # Identify contacts who ENGAGED during the snooze (any paused_at step).
    # A paused_at row is the well-defined 'reply detected, halt cadence'
    # signal — set by pause_sequence() from the inbound webhook. We do
    # NOT regenerate for these contacts; they explicitly engaged, and
    # blasting them with a "circling back" cold email would un-do the
    # whole point of the reply-pause signal. Code-review #1.
    engaged_contact_ids: set[int] = set()
    if contact_ids:
        engaged_rows = (await db.execute(
            select(GeneratedEmail.contact_id).where(
                GeneratedEmail.contact_id.in_(contact_ids),
                GeneratedEmail.paused_at.is_not(None),
                GeneratedEmail.is_sent == False,
            ).distinct()
        )).scalars().all()
        engaged_contact_ids = set(engaged_rows)
        if engaged_contact_ids:
            logger.info(
                f"Wake on company #{company.id}: skipping regen for "
                f"{len(engaged_contact_ids)} engaged contact(s) — they have "
                f"paused_at steps (reply-detected)."
            )

    # 1) Mark pending unsent NON-PAUSED steps as skipped. Critically, we
    #    EXCLUDE paused_at IS NOT NULL — those are the reply-paused steps
    #    that represent a contact's engagement and must not be wiped.
    if contact_ids:
        from sqlalchemy import update as _update
        await db.execute(
            _update(GeneratedEmail)
            .where(
                GeneratedEmail.contact_id.in_(contact_ids),
                GeneratedEmail.is_sent == False,
                GeneratedEmail.skipped_at.is_(None),
                GeneratedEmail.paused_at.is_(None),  # preserve reply-pause signal
            )
            .values(skipped_at=now, skip_reason="regenerated_post_snooze")
        )

    # 2) For each contact NOT engaged, materialize a fresh
    #    DEFAULT_30DAY_TEMPLATE. start_sequence_from_template handles
    #    opt-out / no-email contacts + pre-generates email bodies for
    #    non-day-0 steps.
    total_created = 0
    for contact in contacts:
        if contact.unsubscribed_at:
            continue
        if contact.id in engaged_contact_ids:
            continue  # engaged contact — leave their existing paused steps alone
        try:
            n = await start_sequence_from_template(db, contact, template=DEFAULT_30DAY_TEMPLATE)
            total_created += n
        except Exception as e:
            logger.warning(f"Wake regenerate failed for contact #{contact.id}: {e}")
            continue

        # Flush so the rows from start_sequence_from_template are visible
        # to the next SELECT (autoflush timing varies; this avoids the
        # race documented in code-review #3).
        await db.flush()

        # 3) Replace the freshly-created day-0 "cold" email with the
        #    re-engagement copy. Match by sequence_order=1 (deterministic
        #    in DEFAULT_30DAY_TEMPLATE) instead of id.desc() (which races
        #    with start_sequence_from_template's insert ordering when
        #    multiple steps are pre-generated). Code-review #3.
        freshly_made = (await db.execute(
            select(GeneratedEmail)
            .where(
                GeneratedEmail.contact_id == contact.id,
                GeneratedEmail.is_sent == False,
                GeneratedEmail.skipped_at.is_(None),
                GeneratedEmail.step_type == "email",
                GeneratedEmail.sequence_order == 1,
            )
            .order_by(GeneratedEmail.id.desc())
            .limit(1)
        )).scalar_one_or_none()
        if freshly_made:
            reeng = _re_engagement_first_step(contact, company, snooze_days, snooze_reason)
            freshly_made.subject = reeng["subject"]
            freshly_made.body = reeng["body"]
            freshly_made.email_type = "re_engagement"
        else:
            # Loudly log — if the day-0 step isn't found, the prospect
            # gets the generic cold-intro instead of the agreed
            # 'circling back as promised' copy. Surface this so we can
            # debug before any rep gets confused feedback from a prospect.
            logger.warning(
                f"Wake on company #{company.id} contact #{contact.id}: "
                f"could not find freshly-created day-0 email to overwrite "
                f"with re-engagement copy. Prospect will get generic cold."
            )

    # 4) Clear snooze fields + log Activity.
    prior_resume_at = company.sequence_resume_at
    company.sequence_resume_at = None
    company.sequence_snoozed_at = None
    company.sequence_snooze_reason = None
    company.sequence_snoozed_by_user_id = None
    # Keep sequence_snooze_days for analytics — but it's safe to clear too.
    company.sequence_snooze_days = None

    reason_tail = f" — reason: {snooze_reason}" if snooze_reason else ""
    db.add(Activity(
        company_id=company.id,
        activity_type="sequence_woke_from_snooze",
        content=(
            f"[Auto] Woke from snooze, regenerated {total_created} step(s). "
            f"Original wake: {prior_resume_at.isoformat() if prior_resume_at else 'manual'}{reason_tail}"
        ),
    ))
    await db.flush()
    return total_created


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


async def resume_sequence(db: AsyncSession, contact_id: int, sequence_label: str = "main", resume_at: datetime = None) -> int:
    """Un-pause and re-anchor scheduling so the sequence picks up from where
    it left off. If resume_at is given, anchor to that future date instead of now.

    Two cases handled:
      (1) At least one unsent step has paused_at set — the canonical "Resume"
          case. Clear paused_at + re-anchor relative to the earliest step.
      (2) No paused steps but the sequence is stalled (earliest unsent step
          has scheduled_send_at in the past) — re-anchor unsent steps to
          today so the engine picks them back up. Surfaces as "Restart from
          today" in the UI.

    Returns the number of steps rewired (0 = nothing to do)."""
    now = resume_at or datetime.now(timezone.utc)
    paused_rows = (await db.execute(
        select(GeneratedEmail).where(
            GeneratedEmail.contact_id == contact_id,
            GeneratedEmail.sequence_label == sequence_label,
            GeneratedEmail.is_sent == False,
            GeneratedEmail.paused_at != None,
            GeneratedEmail.skipped_at.is_(None),
        ).order_by(GeneratedEmail.sequence_order)
    )).scalars().all()

    target_rows = paused_rows
    was_paused = bool(paused_rows)

    if not target_rows:
        # Stalled case — pick up unsent non-paused steps if the earliest
        # one is in the past.
        unsent_rows = (await db.execute(
            select(GeneratedEmail).where(
                GeneratedEmail.contact_id == contact_id,
                GeneratedEmail.sequence_label == sequence_label,
                GeneratedEmail.is_sent == False,
                GeneratedEmail.paused_at.is_(None),
                GeneratedEmail.skipped_at.is_(None),
            ).order_by(GeneratedEmail.sequence_order)
        )).scalars().all()
        if not unsent_rows:
            return 0
        # Only re-anchor if the earliest unsent step is in the past (or
        # explicit resume_at was passed — caller wants to force restart).
        earliest = unsent_rows[0].scheduled_send_at
        if earliest and earliest.tzinfo is None:
            earliest = earliest.replace(tzinfo=timezone.utc)
        is_stalled = (not earliest) or (earliest < datetime.now(timezone.utc))
        if not is_stalled and resume_at is None:
            return 0  # sequence is healthy and scheduled for the future — leave alone
        target_rows = unsent_rows

    base_day = target_rows[0].send_delay_days or 0
    base_time = now
    for r in target_rows:
        offset_days = (r.send_delay_days or 0) - base_day
        r.scheduled_send_at = base_time + timedelta(days=max(offset_days, 0))
        r.paused_at = None
    db.add(Activity(
        company_id=target_rows[0].company_id, contact_id=contact_id,
        activity_type="sequence_resumed",
        content=(
            f"[Auto] Sequence resumed — {len(target_rows)} steps re-scheduled from now"
            if was_paused
            else f"[Auto] Sequence restarted — {len(target_rows)} stalled steps re-anchored to today"
        ),
    ))
    return len(target_rows)


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
        # Prefer the admin-editable DB template marked is_default=True.
        # Falls back to the in-code constant if the table is empty (fresh
        # DB before the seed migration runs).
        default_tmpl = (await db.execute(
            select(SequenceTemplate).where(
                SequenceTemplate.is_default == True,
                SequenceTemplate.is_active == True,
            ).order_by(SequenceTemplate.id.desc()).limit(1)
        )).scalar_one_or_none()
        if default_tmpl:
            try:
                template = json.loads(default_tmpl.steps_json)
            except (TypeError, ValueError):
                template = DEFAULT_30DAY_TEMPLATE
        else:
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
        # Pre-evaluate skip conditions at creation time. If the contact
        # is missing the required channel (no LinkedIn, no phone, etc.),
        # skip the step immediately rather than creating it as pending —
        # BDRs shouldn't see steps they can't act on.
        skip_conds = tstep.get("skip_if", [])
        skip_reason = evaluate_skip(contact, skip_conds) if skip_conds else None
        if skip_reason:
            # Still create the step so the timeline is complete, but mark
            # it skipped from the start
            step = GeneratedEmail(
                contact_id=contact.id,
                company_id=company.id,
                step_type=tstep["step_type"],
                email_type=tstep["label"],
                subject=f"[Skipped] {tstep['step_type'].title()} step {idx}",
                body=f"Skipped at creation: {skip_reason}",
                sequence_order=idx,
                send_delay_days=tstep["day"],
                scheduled_send_at=now + timedelta(days=tstep["day"]),
                skip_if_json=json.dumps(skip_conds),
                auto_execute=False,
                sequence_label=sequence_label,
                skipped_at=now,
                skip_reason=skip_reason,
            )
            db.add(step)
            created += 1
            continue

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
            # Show both contact phone and company main line so the BDR has options
            contact_phone = (contact.phone or "").strip()
            company_phone = (company.phone or "").strip()
            phone_line = ""
            if contact_phone and company_phone and contact_phone != company_phone:
                phone_line = f"📞 Direct: {contact_phone} | Main: {company_phone}\n\n"
            elif contact_phone:
                phone_line = f"📞 {contact_phone}\n\n"
            elif company_phone:
                phone_line = f"📞 Company main line: {company_phone}\n\n"
            subject = f"Call {idx}"
            body = (
                f"{phone_line}"
                f"Call talk track:\n\n"
                f"- Hi {contact.first_name or 'there'} — from Backyard Marketing Pros.\n"
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
        # Snap freshly-generated steps to the org send window
        from app.services.send_window import snap_pending_steps_to_window
        await snap_pending_steps_to_window(db, contact_id=contact.id)
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
