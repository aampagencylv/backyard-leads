"""
Email Sending Service via Resend
Handles sending emails, tracking opens, and processing webhooks.
"""
from __future__ import annotations
from typing import Optional
import re as _re
import httpx
from datetime import datetime, timezone
from app.config import settings


# ============================================================
# Anomaly scoring + audit logging
# ============================================================
#
# Three failure modes the existing guards in send_email() catch
# explicitly:
#   - step_type != 'email'
#   - subject matches placeholder regex
#   - body starts with talk-track / DM marker
#
# Beyond those, an anomaly SCORE catches general weirdness without
# enumerating every possible failure: ultra-short bodies, body that
# is only ALL CAPS, placeholder-y filler text, mismatched recipients.
# Score >= 60 → refuse + log. Score >= 30 → allow but flag in audit.

_SUBJECT_PLACEHOLDER_RE = _re.compile(
    # Three placeholder shapes we know about:
    #   - "[Skipped] ..."
    #   - "Call 3" / "iMessage step 5" / "LinkedIn step 2" (exact)
    # The "LinkedIn message: <name>" alternative was REMOVED 2026-06-04
    # because re.match treated it as a prefix and refused legitimate BDR
    # subjects beginning "LinkedIn message: re your Sedona pool post".
    # The body marker + step_type guards still catch the placeholder case.
    # Code-review #6.
    r"^(\s*\[skipped\]|\s*(?:call|imessage|linkedin)\s+(?:step\s+)?\d+\s*$)",
    _re.I,
)
_BODY_NONEMAIL_PREFIXES = ("📞", "Connect note (under 280 chars):", "Connect note:")


def _score_email_anomaly(subject: str, body: str, recipient_email: str) -> tuple[int, list[str]]:
    """Score how 'weird-looking' an outbound email is. Returns (score, flags).
    Score range: 0 (clean) → 100 (clearly garbage). Flags are short
    human-readable strings so the audit log row + digest can explain why."""
    score = 0
    flags: list[str] = []

    s = (subject or "").strip()
    b = (body or "").strip()

    # Subject signals
    if not s:
        score += 40; flags.append("empty_subject")
    if s and _SUBJECT_PLACEHOLDER_RE.match(s):
        score += 50; flags.append("placeholder_subject")
    if len(s) > 0 and len(s) <= 6:
        score += 15; flags.append("tiny_subject")
    if s and s.isupper() and len(s) > 8:
        score += 20; flags.append("all_caps_subject")
    if s and "step" in s.lower() and _re.search(r"\b\d+\b", s):
        score += 25; flags.append("subject_has_step_n")
    if s and s.startswith("["):
        score += 20; flags.append("subject_starts_bracket")

    # Body signals
    if not b:
        score += 50; flags.append("empty_body")
    if b and b.startswith(_BODY_NONEMAIL_PREFIXES):
        score += 60; flags.append("non_email_body_marker")
    if 0 < len(b) < 80:
        score += 25; flags.append("ultra_short_body")
    if b and b.lower().startswith("connect note"):
        score += 50; flags.append("linkedin_body_marker")
    # All-caps body (more than 5 ALL-CAPS words in a row)
    if b and _re.search(r"\b[A-Z]{4,}\b(\s+\b[A-Z]{4,}\b){4,}", b):
        score += 20; flags.append("all_caps_run")
    # Body still has unsubstituted template placeholders {{like_this}}
    if "{{" in b and "}}" in b:
        score += 35; flags.append("unsubstituted_template_var")

    # Recipient sanity — covers obvious test-data leaks
    if not recipient_email or "@" not in recipient_email:
        score += 50; flags.append("invalid_recipient")
    elif recipient_email.lower().endswith(("@test.com", "@example.com", "@localhost")):
        score += 30; flags.append("test_domain_recipient")

    return min(score, 100), flags


async def _log_outbound_audit(
    *,
    sender_user_id: Optional[int],
    company_id: int,
    contact_id: int,
    email_id: int,
    step_type: Optional[str],
    subject: str,
    body: str,
    recipient_email: str,
    status: str,                    # 'sent' | 'blocked' | 'failed' | 'transient'
    blocked_reason: Optional[str] = None,
    anomaly_score: int = 0,
    anomaly_flags: Optional[list[str]] = None,
    resend_id: Optional[str] = None,
    error_message: Optional[str] = None,
    caller_module: Optional[str] = None,
) -> None:
    """Write one row to outbound_email_audit per send_email() call.

    All inserts are best-effort — never raise. We never want an audit
    failure to break a real outbound send (or block a guard return).
    """
    try:
        from sqlalchemy import text
        from app.database import async_session

        # Resolve tenant via company row when possible (audit needs tenant scope).
        tenant_id: Optional[int] = None
        async with async_session() as s:
            if company_id:
                row = await s.execute(
                    text("SELECT tenant_id FROM companies WHERE id = :c"),
                    {"c": company_id},
                )
                r = row.fetchone()
                if r:
                    tenant_id = r[0]
            await s.execute(text("""
                INSERT INTO outbound_email_audit (
                    tenant_id, sender_user_id, company_id, contact_id, email_id,
                    step_type, subject, body_preview, recipient_email,
                    status, blocked_reason, anomaly_score, anomaly_flags,
                    resend_id, error_message, caller_module
                ) VALUES (
                    :tenant_id, :sender_user_id, :company_id, :contact_id, :email_id,
                    :step_type, :subject, :body_preview, :recipient_email,
                    :status, :blocked_reason, :anomaly_score, :anomaly_flags,
                    :resend_id, :error_message, :caller_module
                )
            """), {
                "tenant_id": tenant_id,
                "sender_user_id": sender_user_id,
                "company_id": company_id or None,
                "contact_id": contact_id or None,
                "email_id": email_id or None,
                "step_type": step_type,
                "subject": (subject or "")[:500],
                "body_preview": (body or "")[:300],
                "recipient_email": (recipient_email or "")[:320],
                "status": status,
                "blocked_reason": blocked_reason[:200] if blocked_reason else None,
                "anomaly_score": int(anomaly_score),
                "anomaly_flags": (",".join(anomaly_flags) if anomaly_flags else None),
                "resend_id": resend_id,
                "error_message": error_message[:2000] if error_message else None,
                "caller_module": (caller_module or "")[:120] or None,
            })
            await s.commit()
    except Exception as e:
        import logging
        logging.getLogger("bmp.outbound_audit").warning(
            f"audit write failed (non-fatal): {type(e).__name__}: {e}"
        )


def _compliance_footer() -> str:
    """
    Minimal compliance footer. Postal address only (CAN-SPAM requirement).
    No visible unsubscribe link — Gmail/Outlook handle that via the
    List-Unsubscribe HTTP header (set in the Resend payload), which surfaces
    as a native button at the top of the email instead of footer copy.
    Visible footer "click to unsubscribe" links trigger Gmail Promotions
    classification and hurt inbox placement.
    """
    return f"""
    <div style="margin-top:20px;font-size:11px;color:#999;font-family:Arial,sans-serif;">
        {settings.bmp_postal_address}
    </div>
    """


async def send_email(
    to_email: str,
    subject: str,
    body: str,
    from_name: str,
    from_firstname: str,
    reply_to_email: str,
    company_id: int,
    contact_id: int,
    email_id: int,
    signature_html: str = "",
    unsubscribe_token: str | None = None,
    step_type: str | None = None,
    engagement_action_id: int | None = None,
) -> dict:
    # ----------------------------------------------------------------
    # Defense in depth: refuse to send obvious non-email content.
    #
    # Incident 2026-06-03: two manual send routes (send_single_email,
    # send_next_in_sequence) lacked a step_type filter. A BDR clicking
    # 'Send' on a contact whose next pending row was a call/linkedin/
    # imessage step would happily dispatch that row's PLACEHOLDER subject
    # ('Call 3', 'iMessage step 5', '[Skipped] Linkedin step 2') and
    # talk-track / DM body to the prospect's email inbox via Resend.
    # 439 bad sends across 275 contacts before discovery.
    #
    # The route-level fix is the primary defense. These guards are the
    # belt + suspenders so a future route that forgets to filter STILL
    # can't reach Resend with non-email content.
    # ----------------------------------------------------------------
    # STAGING SAFETY GUARD — when STAGING_FORCE_RECIPIENT is set in the
    # environment (staging only — NEVER set in prod), every outbound
    # email's recipient gets rewritten to that single staging mailbox.
    # This makes staging code STRUCTURALLY incapable of emailing a real
    # prospect, even if PII sanitization missed a row or a bug introduces
    # a real address from outside the DB. The original intended recipient
    # is logged + appended to the subject so we can verify routing in
    # the staging mailbox.
    import os
    _force_to = os.environ.get("STAGING_FORCE_RECIPIENT", "").strip()
    if _force_to:
        original_to = to_email
        to_email = _force_to
        # Prefix subject so the audit log + recipient inbox show original target
        subject = f"[STAGING → was: {original_to}] {subject}"
        import logging
        logging.getLogger("bmp.email_sender").info(
            f"STAGING_FORCE_RECIPIENT active — rewrote to_email "
            f"{original_to!r} → {to_email!r} for email_id={email_id}"
        )

    # Compute anomaly score ONCE — used by guards + audit log + digest.
    anomaly_score, anomaly_flags = _score_email_anomaly(subject, body, to_email)

    # Identify the calling module for audit trail (helps post-incident triage —
    # tells us if a bad send came from send_routes vs sequence_engine vs audit_routes etc.)
    import inspect
    _caller = None
    try:
        _frame = inspect.currentframe()
        if _frame and _frame.f_back:
            _caller = f"{_frame.f_back.f_globals.get('__name__', '?')}:{_frame.f_back.f_lineno}"
    except Exception:
        pass

    # Lookup sender_user_id from the X-Sender header (set higher up) —
    # actually we don't have it on this call signature, so leave NULL.
    # The audit log query joins via company.assigned_to for reporting if needed.
    _audit_base = dict(
        sender_user_id=None,
        company_id=company_id, contact_id=contact_id, email_id=email_id,
        step_type=step_type, subject=subject or "", body=body or "",
        recipient_email=to_email, anomaly_score=anomaly_score,
        anomaly_flags=anomaly_flags, caller_module=_caller,
    )

    async def _refuse(reason_short: str, error_msg: str) -> dict:
        import logging
        logging.getLogger("bmp.email_sender").error(
            f"send_email REFUSED [{reason_short}] — "
            f"email_id={email_id} company={company_id} contact={contact_id} "
            f"subject={subject!r} score={anomaly_score} flags={anomaly_flags}"
        )
        await _log_outbound_audit(**_audit_base, status="blocked",
                                  blocked_reason=reason_short, error_message=error_msg)
        return {"success": False, "error": error_msg, "blocked_by_guard": True,
                "anomaly_score": anomaly_score, "anomaly_flags": anomaly_flags}

    # Hard guards (route-level + caller-level explicit signals)
    if step_type and step_type != "email":
        return await _refuse("step_type_not_email",
                             f"step_type={step_type} cannot be sent through send_email — caller bug")
    # Empty subject = guaranteed bad. Gmail/Outlook penalize empty-subject
    # senders aggressively for reputation; the soft anomaly score (40)
    # was below the 60 hard-threshold so empties were dispatching.
    # Code-review #5.
    if not (subject or "").strip():
        return await _refuse("empty_subject",
                             "Refused to send email with empty subject (mailbox-provider penalty)")
    if subject and _SUBJECT_PLACEHOLDER_RE.match(subject):
        return await _refuse("placeholder_subject",
                             f"Refused to send placeholder subject: {subject!r}")
    body_stripped = (body or "").lstrip()
    if body_stripped.startswith(_BODY_NONEMAIL_PREFIXES):
        return await _refuse("non_email_body",
                             "Refused to send non-email content (call talk-track or LinkedIn DM detected)")
    # Soft guard — anomaly score threshold. The hard guards above are
    # exact matches for known leak patterns; this one catches NEW failure
    # modes we haven't enumerated. 60 is permissive — only refuses when
    # several signals fire together.
    if anomaly_score >= 60:
        return await _refuse("anomaly_score_high",
                             f"Refused: anomaly score {anomaly_score} with flags {anomaly_flags}")

    from_address = f"{from_name} <{from_firstname}@{settings.send_domain}>"

    # Render any markdown links [text](url) → <a> anchors. Normally the
    # caller's wrap_html_links already did this (and tracked the href),
    # so this is a no-op; but if click-wrapping was skipped or failed,
    # this guarantees the audit CTA still renders as a real hyperlink
    # in the inbox instead of literal "[View ...](https://...)" text.
    try:
        from app.services.tracking import linkify_markdown
        body = linkify_markdown(body)
    except Exception:
        pass

    body_html = body.replace("\n", "<br>")
    sig_block = f'<div style="margin-top:24px">{signature_html}</div>' if signature_html else ""
    footer = _compliance_footer()
    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; color: #333; line-height: 1.6;">
        {body_html}
        {sig_block}
        {footer}
    </div>
    """

    headers = {
        "X-Company-ID": str(company_id),
        "X-Contact-ID": str(contact_id),
        "X-Email-ID": str(email_id),
    }
    # Include engagement_action_id as a header too — Resend's webhook
    # payload sends tags as a dict, not a list, and our handler's
    # `for t in tags: t["name"]` parsing returns {} on that shape.
    # X-headers come through cleanly as a list in the webhook payload
    # so this header is the reliable attribution channel for engine
    # opens/clicks/bounces. Without it, every new-engine email's
    # Resend webhook events were routing only via the legacy email_id
    # path → 0 signals written → decision_maker had nothing to react to.
    if engagement_action_id is not None:
        headers["X-Engagement-Action-ID"] = str(engagement_action_id)
    # List-Unsubscribe headers — invisible to recipient, but Gmail/Outlook
    # use them to render a native unsubscribe button at the top of the email
    # AND treat the sender as more legitimate (better inbox placement).
    if unsubscribe_token:
        unsub_url = f"{settings.public_url.rstrip('/')}/unsubscribe?t={unsubscribe_token}"
        headers["List-Unsubscribe"] = f"<{unsub_url}>"
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    # Wrap the Reply-To with the sender's display name so the recipient's email
    # client shows "Steven Edwards" instead of the raw r-<token>@go.bymp.com
    # address when they hit Reply. The token is still in the address for routing,
    # but it's tucked behind the human name. Most clients (Gmail, Apple Mail,
    # Outlook, etc.) honor the display name in Reply-To headers.
    reply_to_value = f"{from_name} <{reply_to_email}>" if from_name else reply_to_email

    # Plain-text alternative — HTML-only emails are a soft spam signal at
    # Gmail/Outlook, and a clean text part also produces sane quoted-reply
    # output. Derive from the same HTML so the two versions never diverge.
    try:
        from app.services.html_to_text import html_to_plain_text
        text_body = html_to_plain_text(html_body)
    except Exception:
        text_body = body  # last-ditch fallback to the raw input

    # Pre-send spam score — log it on every send so the reputation
    # dashboard can later correlate score → bounce/complaint rates.
    # Never blocks the send; this is observability, not enforcement.
    try:
        from app.services.spam_score import score_email
        _sc = score_email(subject=subject, html_body=html_body, plain_body=text_body)
        import logging
        _level = {"ok": logging.DEBUG, "watch": logging.INFO, "high": logging.WARNING}.get(_sc["bucket"], logging.INFO)
        logging.getLogger("bmp.spam_score").log(
            _level,
            f"score={_sc['score']} bucket={_sc['bucket']} email_id={email_id} contact={contact_id}"
            + (f" issues={[i['kind'] for i in _sc['issues']]}" if _sc["issues"] else "")
        )
    except Exception:
        pass
    payload = {
        "from": from_address,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
        "text": text_body,
        "reply_to": reply_to_value,
        "headers": headers,
        "tags": (
            [
                {"name": "company_id", "value": str(company_id)},
                {"name": "contact_id", "value": str(contact_id)},
                {"name": "email_id", "value": str(email_id)},
            ]
            # When this send came from the new engagement engine, add a
            # distinct tag so the webhook routes it to actions/signals
            # instead of generated_emails. action.id and generated_emails.id
            # share the same INT space and overlap heavily (~2000 collisions
            # on prod at cutover), so the email_id tag alone is ambiguous.
            + ([{"name": "engagement_action_id",
                 "value": str(engagement_action_id)}]
               if engagement_action_id is not None else [])
        ),
    }

    # Structured timeout. Resend's body delivery is usually <2s but the
    # response-header phase can take longer under their load — bump read
    # to 30s while keeping connect short so we fail fast on DNS/network.
    # The whole call is wrapped in a broad except so a transient Resend
    # blip doesn't bubble up as an unhandled exception (Sentry incident
    # 970c574 from 2026-06-03 was a ReadTimeout that escaped this path).
    # Transient failures return success=False with retryable=True; the
    # sequence engine treats those as "leave the step pending, retry on
    # next tick" instead of marking the step errored.
    timeout = httpx.Timeout(connect=8.0, read=30.0, write=15.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
        # Transient — Resend was slow / network hiccup. Engine should retry.
        import logging
        logging.getLogger("bmp.email_sender").warning(
            f"Resend transient failure for email_id={email_id}: {type(e).__name__}: {e}"
        )
        await _log_outbound_audit(**_audit_base, status="transient",
                                  error_message=f"{type(e).__name__}: {e}")
        return {"success": False, "error": f"Resend transient: {type(e).__name__}", "retryable": True,
                "anomaly_score": anomaly_score, "anomaly_flags": anomaly_flags}
    except httpx.HTTPError as e:
        import logging
        logging.getLogger("bmp.email_sender").exception(
            f"Resend HTTPError for email_id={email_id}: {type(e).__name__}: {e}"
        )
        await _log_outbound_audit(**_audit_base, status="failed",
                                  error_message=f"{type(e).__name__}: {e}")
        return {"success": False, "error": f"{type(e).__name__}: {e}", "retryable": False,
                "anomaly_score": anomaly_score, "anomaly_flags": anomaly_flags}

    if response.status_code in (200, 201):
        data = response.json()
        resend_id = data.get("id")
        await _log_outbound_audit(**_audit_base, status="sent", resend_id=resend_id)
        return {"success": True, "resend_id": resend_id, "message": "Email sent successfully",
                "anomaly_score": anomaly_score, "anomaly_flags": anomaly_flags}
    # 5xx from Resend = their problem, retry on next tick. 429 is rate
    # limiting (Resend caps ~2 req/s) — also transient by definition; the
    # dispatcher's batch loop trips it routinely, so treating it as a
    # permanent failure threw away real sends.
    retryable = response.status_code == 429 or 500 <= response.status_code < 600
    await _log_outbound_audit(
        **_audit_base, status=("transient" if retryable else "failed"),
        error_message=f"HTTP {response.status_code}: {(response.text or '')[:500]}",
    )
    return {"success": False, "error": response.text, "status_code": response.status_code,
            "retryable": retryable, "anomaly_score": anomaly_score, "anomaly_flags": anomaly_flags}


def get_sender_info(first_name: str, full_name: str) -> dict:
    """Derive sender email from first name (preferred) or full name."""
    fn = (first_name or "").strip().lower()
    if not fn and full_name:
        fn = full_name.strip().split()[0].lower()
    return {
        "from_name": full_name,
        "from_firstname": fn,
        "from_email": f"{fn}@{settings.send_domain}",
        "reply_to": f"{fn}@{settings.reply_domain}",
    }


def generate_reply_token() -> str:
    """32-char lowercase-hex token for the Reply-To routing address.

    Email local-parts get normalized to lowercase by most mail providers
    (Resend / SES / Postmark all do this). Mixed-case tokens like
    `secrets.token_urlsafe(20)` round-trip through the wire as lowercase
    and our DB lookup misses. Hex is always lowercase → no normalization
    issue. 16 random bytes = 128 bits of entropy, plenty for a routing key."""
    import secrets
    return secrets.token_hex(16)


def reply_to_for_token(token: str) -> str:
    """Build the Reply-To address for a given token.

    `r-` prefix is what the inbound webhook parser splits on to extract
    the token. Keeps the local-part visually identifiable as a system
    address so a savvy prospect can see what's going on if they look,
    but doesn't reveal anything sensitive."""
    return f"r-{token}@{settings.inbound_reply_domain}"
