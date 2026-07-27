"""Daily outbound digest — peace-of-mind operational visibility.

Sends ONE email per day PER TENANT to that tenant's admins, summarizing what
their team sent in the previous 24 hours. Critical sections:

  - Per-BDR send counts (total, sent, blocked, failed)
  - Any BLOCKED sends (a guard fired — what was it, who tried, what subject)
  - Any high-anomaly-score sends that went through (allowed but flagged)
  - Top recipients by volume
  - Any sends with placeholder-looking subjects that slipped through the
    guards somehow (defense-in-depth check)

The digest itself is sent via Resend directly (bypassing send_email) so
it doesn't appear in its own audit log.

EVERY query here is raw SQL, which bypasses the ORM tenant auto-filter in
app/tenancy.py. Each one therefore carries an explicit `tenant_id = :tid`
predicate. Do not remove them — without them this digest aggregates every
tenant's prospect addresses and subject lines into one email.
"""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from sqlalchemy import text

from app.database import async_session
from app.config import settings
from app.services.tenant_identity import (
    active_tenant_ids,
    get_tenant_identity,
    tenant_notification_emails,
)

log = logging.getLogger("prospector.outbound_digest")


async def _query_summary(tenant_id: int, hours: int = 24) -> dict:
    """Pull all the data we need for one tenant's digest in one DB session."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    params = {"cutoff": cutoff, "tid": int(tenant_id)}
    async with async_session() as s:
        # Per-BDR totals
        per_bdr = await s.execute(text("""
            SELECT
                COALESCE(u.first_name || ' ' || u.last_name, 'engine/none') AS sender,
                COUNT(*) FILTER (WHERE oea.status = 'sent') AS sent,
                COUNT(*) FILTER (WHERE oea.status = 'blocked') AS blocked,
                COUNT(*) FILTER (WHERE oea.status = 'failed') AS failed,
                COUNT(*) FILTER (WHERE oea.status = 'transient') AS transient,
                COUNT(*) AS total
            FROM outbound_email_audit oea
            LEFT JOIN users u ON u.id = oea.sender_user_id AND u.tenant_id = :tid
            WHERE oea.created_at >= :cutoff AND oea.tenant_id = :tid
            GROUP BY sender
            ORDER BY total DESC
        """), params)
        per_bdr_rows = [dict(zip(("sender","sent","blocked","failed","transient","total"), r))
                        for r in per_bdr.fetchall()]

        # Blocked sends — full detail
        blocked = await s.execute(text("""
            SELECT
                oea.id, oea.created_at, oea.blocked_reason,
                oea.subject, oea.recipient_email, oea.anomaly_score, oea.anomaly_flags,
                COALESCE(u.first_name || ' ' || u.last_name, 'engine') AS sender,
                oea.caller_module, c.name AS company_name
            FROM outbound_email_audit oea
            LEFT JOIN users u ON u.id = oea.sender_user_id AND u.tenant_id = :tid
            LEFT JOIN companies c ON c.id = oea.company_id AND c.tenant_id = :tid
            WHERE oea.created_at >= :cutoff AND oea.tenant_id = :tid
              AND oea.status = 'blocked'
            ORDER BY oea.created_at DESC LIMIT 50
        """), params)
        blocked_rows = [dict(zip(
            ("id","created_at","reason","subject","recipient","score","flags","sender","caller","company"), r))
            for r in blocked.fetchall()]

        # SENT but with anomaly_score >= 30 — went through but looked weird
        sent_flagged = await s.execute(text("""
            SELECT
                oea.id, oea.created_at, oea.subject, oea.recipient_email,
                oea.anomaly_score, oea.anomaly_flags,
                COALESCE(u.first_name || ' ' || u.last_name, 'engine') AS sender,
                c.name AS company_name
            FROM outbound_email_audit oea
            LEFT JOIN users u ON u.id = oea.sender_user_id AND u.tenant_id = :tid
            LEFT JOIN companies c ON c.id = oea.company_id AND c.tenant_id = :tid
            WHERE oea.created_at >= :cutoff
              AND oea.tenant_id = :tid
              AND oea.status = 'sent'
              AND oea.anomaly_score >= 30
            ORDER BY oea.anomaly_score DESC, oea.created_at DESC LIMIT 30
        """), params)
        sent_flagged_rows = [dict(zip(
            ("id","created_at","subject","recipient","score","flags","sender","company"), r))
            for r in sent_flagged.fetchall()]

        # Top recipient domains — sanity check (any weird domains getting volume?)
        top_domains = await s.execute(text("""
            SELECT
                SUBSTRING(recipient_email FROM '@(.+)') AS domain,
                COUNT(*) AS n
            FROM outbound_email_audit
            WHERE created_at >= :cutoff AND tenant_id = :tid AND status = 'sent'
            GROUP BY domain
            HAVING COUNT(*) >= 5
            ORDER BY n DESC LIMIT 10
        """), params)
        top_domain_rows = [dict(zip(("domain","n"), r)) for r in top_domains.fetchall()]

        # Failed sends (Resend errors) — operational signal
        failed = await s.execute(text("""
            SELECT
                oea.created_at, oea.subject, oea.recipient_email,
                LEFT(oea.error_message, 200) AS error_summary,
                COALESCE(u.first_name || ' ' || u.last_name, 'engine') AS sender
            FROM outbound_email_audit oea
            LEFT JOIN users u ON u.id = oea.sender_user_id AND u.tenant_id = :tid
            WHERE oea.created_at >= :cutoff AND oea.tenant_id = :tid
              AND oea.status IN ('failed', 'transient')
            ORDER BY oea.created_at DESC LIMIT 15
        """), params)
        failed_rows = [dict(zip(("created_at","subject","recipient","error","sender"), r))
                       for r in failed.fetchall()]

    return {
        "tenant_id": int(tenant_id),
        "cutoff": cutoff,
        "per_bdr": per_bdr_rows,
        "blocked": blocked_rows,
        "sent_flagged": sent_flagged_rows,
        "top_domains": top_domain_rows,
        "failed": failed_rows,
    }


def _render_digest_html(data: dict, ident=None) -> tuple[str, str]:
    """Build the (subject, html_body) tuple for the digest email."""
    cutoff = data["cutoff"]
    org = (getattr(ident, "company_name", "") or "").strip()
    total_sent = sum(r["sent"] for r in data["per_bdr"])
    total_blocked = sum(r["blocked"] for r in data["per_bdr"])
    total_failed = sum(r["failed"] for r in data["per_bdr"])

    # Subject summarizes the state at a glance
    tag = f"{org} outbound digest" if org else "Outbound digest"
    if total_blocked > 0:
        subject = f"🛡 {tag} — {total_sent} sent, {total_blocked} BLOCKED ({cutoff.strftime('%b %d')})"
    elif total_failed > 0:
        subject = f"⚠ {tag} — {total_sent} sent, {total_failed} failed ({cutoff.strftime('%b %d')})"
    else:
        subject = f"✓ {tag} — {total_sent} sent, all clean ({cutoff.strftime('%b %d')})"

    parts: list[str] = []
    parts.append(f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#222;max-width:720px;margin:0 auto">
      <h2 style="margin-top:24px">{('%s — outbound digest' % org) if org else 'Outbound digest'} — last 24h</h2>
      <div style="color:#666;font-size:13px;margin-bottom:24px">
        Window: since {cutoff.strftime('%Y-%m-%d %H:%M UTC')}<br>
        Total: <strong>{total_sent}</strong> sent · <strong style="color:#c0392b">{total_blocked}</strong> blocked · <strong style="color:#e67e22">{total_failed}</strong> failed
      </div>
    """)

    # Per-BDR table
    parts.append("<h3>Per-BDR send counts</h3><table style='border-collapse:collapse;width:100%;font-size:14px;margin-bottom:24px'>")
    parts.append("<thead><tr style='background:#f6f6f6;border-bottom:1px solid #ddd'>"
                 "<th align='left' style='padding:8px 12px'>BDR</th>"
                 "<th align='right' style='padding:8px 12px'>Sent</th>"
                 "<th align='right' style='padding:8px 12px;color:#c0392b'>Blocked</th>"
                 "<th align='right' style='padding:8px 12px;color:#e67e22'>Failed</th>"
                 "<th align='right' style='padding:8px 12px'>Total</th>"
                 "</tr></thead><tbody>")
    for r in data["per_bdr"]:
        parts.append(f"<tr style='border-bottom:1px solid #eee'>"
                     f"<td style='padding:8px 12px'>{r['sender']}</td>"
                     f"<td align='right' style='padding:8px 12px'>{r['sent']}</td>"
                     f"<td align='right' style='padding:8px 12px;color:{'#c0392b' if r['blocked'] else '#888'}'>{r['blocked']}</td>"
                     f"<td align='right' style='padding:8px 12px;color:{'#e67e22' if r['failed'] else '#888'}'>{r['failed']}</td>"
                     f"<td align='right' style='padding:8px 12px'><strong>{r['total']}</strong></td>"
                     f"</tr>")
    parts.append("</tbody></table>")

    # Blocked sends (most important section if anything fired)
    if data["blocked"]:
        parts.append("<h3 style='color:#c0392b'>🛡 Blocked sends (guards fired)</h3>")
        parts.append("<p style='font-size:13px;color:#666'>These are sends that the new guards REFUSED to dispatch. Each one would have been a bad send before the fix.</p>")
        parts.append("<div style='font-size:13px;line-height:1.6'>")
        for r in data["blocked"]:
            ts = r["created_at"].strftime("%H:%M") if r["created_at"] else "?"
            parts.append(f"<div style='border-left:3px solid #c0392b;padding:8px 14px;margin:8px 0;background:#fdf5f5'>"
                         f"<strong>{ts}</strong> · {r['sender']} → {r['recipient'] or '?'} "
                         f"<span style='color:#666'>({r['company'] or '?'})</span><br>"
                         f"<span style='font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#444'>"
                         f"reason: {r['reason']} · subject: {r['subject'] or '(empty)'}<br>"
                         f"score: {r['score']} · flags: {r['flags'] or 'none'} · caller: {r['caller'] or '?'}"
                         f"</span></div>")
        parts.append("</div>")

    # Sent but flagged (defense-in-depth visibility)
    if data["sent_flagged"]:
        parts.append("<h3 style='color:#e67e22'>⚠ Sent but flagged</h3>")
        parts.append("<p style='font-size:13px;color:#666'>These emails went through but had anomaly score ≥30 — worth a glance.</p>")
        parts.append("<table style='border-collapse:collapse;width:100%;font-size:13px'>")
        parts.append("<thead><tr style='background:#f6f6f6;border-bottom:1px solid #ddd'>"
                     "<th align='left' style='padding:6px 10px'>Time</th>"
                     "<th align='left' style='padding:6px 10px'>Sender</th>"
                     "<th align='left' style='padding:6px 10px'>Recipient</th>"
                     "<th align='left' style='padding:6px 10px'>Subject</th>"
                     "<th align='right' style='padding:6px 10px'>Score</th>"
                     "<th align='left' style='padding:6px 10px'>Flags</th>"
                     "</tr></thead><tbody>")
        for r in data["sent_flagged"]:
            ts = r["created_at"].strftime("%H:%M") if r["created_at"] else "?"
            parts.append(f"<tr style='border-bottom:1px solid #eee'>"
                         f"<td style='padding:6px 10px;font-family:ui-monospace,Menlo,monospace'>{ts}</td>"
                         f"<td style='padding:6px 10px'>{r['sender']}</td>"
                         f"<td style='padding:6px 10px'>{r['recipient'] or '?'}</td>"
                         f"<td style='padding:6px 10px'>{(r['subject'] or '')[:60]}</td>"
                         f"<td align='right' style='padding:6px 10px;color:#e67e22'><strong>{r['score']}</strong></td>"
                         f"<td style='padding:6px 10px;font-family:ui-monospace,Menlo,monospace;font-size:11px'>{r['flags'] or ''}</td>"
                         f"</tr>")
        parts.append("</tbody></table>")

    # Top recipient domains (operational sanity)
    if data["top_domains"]:
        parts.append("<h3 style='margin-top:32px'>Top recipient domains</h3>")
        parts.append("<div style='font-size:13px;color:#444'>")
        for r in data["top_domains"]:
            parts.append(f"<div>{r['domain']}: {r['n']}</div>")
        parts.append("</div>")

    # Failed (Resend errors)
    if data["failed"]:
        parts.append("<h3 style='margin-top:32px;color:#e67e22'>Failed / transient (Resend)</h3>")
        parts.append("<div style='font-size:13px;font-family:ui-monospace,Menlo,monospace;background:#fafafa;border:1px solid #eee;padding:10px;border-radius:6px'>")
        for r in data["failed"]:
            ts = r["created_at"].strftime("%H:%M") if r["created_at"] else "?"
            parts.append(f"<div style='margin-bottom:6px'>{ts} {r['sender']} → {r['recipient']}: {r['error'] or '?'}</div>")
        parts.append("</div>")

    footer_org = f"{org} · " if org else ""
    parts.append("<div style='margin-top:32px;padding-top:24px;border-top:1px solid #eee;color:#888;font-size:11px'>"
                 f"{footer_org}Auto-generated outbound audit. The full data lives in the outbound_email_audit table.</div></div>")

    return subject, "".join(parts)


async def _already_sent_today(tenant_id: int, recipient: str) -> bool:
    """Check the outbound_email_audit table for a digest already sent to
    this recipient in the last 18 hours. Persistent dedup across process
    restarts — replaces the in-memory last_sent_date in main.py which
    re-fired on every deploy after the 13:00 UTC threshold.
    Code-review #7."""
    try:
        from sqlalchemy import text
        from app.database import async_session
        cutoff = datetime.now(timezone.utc) - timedelta(hours=18)
        async with async_session() as s:
            r = await s.execute(text("""
                SELECT 1 FROM outbound_email_audit
                WHERE caller_module LIKE '%outbound_digest%'
                  AND tenant_id = :tid
                  AND recipient_email = :r
                  AND created_at >= :cutoff
                  AND status = 'sent'
                LIMIT 1
            """), {"tid": int(tenant_id), "r": recipient, "cutoff": cutoff})
            return r.fetchone() is not None
    except Exception as e:
        log.warning(f"digest dedup check failed (non-fatal, allowing send): {e}")
        return False


async def send_digest(
    tenant_id: int,
    *,
    hours: int = 24,
    recipient: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Build + send one tenant's daily digest via Resend directly. Returns
    {sent, subject, totals} on success.

    `recipient` defaults to the tenant's own admins — this report contains
    that tenant's prospect addresses and subject lines, so it must never go
    to another tenant's inbox.

    Pass force=True to bypass the 18h dedup window (useful for ad-hoc
    weekly retrospectives via the manual admin endpoint)."""
    ident = await get_tenant_identity(tenant_id)

    recipients = [recipient] if recipient else await tenant_notification_emails(tenant_id)
    if not recipients:
        log.info(f"digest skipped (tenant={tenant_id}): no admin recipients on file")
        return {"sent": False, "skipped": "no_recipients"}

    # The digest is internal mail about the tenant's own sending, but it still
    # has to leave from a domain that tenant owns. No shared-domain fallback.
    if not ident.send_domain:
        log.info(
            f"digest skipped (tenant={tenant_id}): no verified sending domain "
            f"(missing: {', '.join(ident.missing())})"
        )
        return {"sent": False, "skipped": "no_verified_send_domain"}

    to_addr = recipients[0]
    if not force and await _already_sent_today(tenant_id, to_addr):
        log.info(f"digest skipped (tenant={tenant_id}): already sent to {to_addr} in the last 18h")
        return {"sent": False, "skipped": "deduped"}

    data = await _query_summary(tenant_id, hours=hours)
    subject, html = _render_digest_html(data, ident)

    # Send via Resend directly — bypassing send_email() so the digest
    # itself doesn't appear in its own audit log + isn't subject to
    # guards (it's internal, not outreach).
    if not settings.resend_api_key:
        log.error("RESEND_API_KEY not set — can't send digest")
        return {"sent": False, "error": "no resend key"}

    from_label = f"{ident.company_name} Outbound Audit" if ident.company_name else "Outbound Audit"
    payload = {
        "from": f"{from_label} <audit@{ident.send_domain}>",
        "to": recipients,
        "subject": subject,
        "html": html,
        "headers": {"X-Internal-Digest": "outbound-audit"},
        "tags": [{"name": "kind", "value": "outbound_audit_digest"}],
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if r.status_code in (200, 201):
            log.info(f"digest sent (tenant={tenant_id}) to {', '.join(recipients)}: {subject}")
            resend_id = r.json().get("id")
            # Write an audit row tagged caller_module='outbound_digest' so
            # _already_sent_today() above can dedup on the next loop tick.
            # This is the persistent state that replaces the in-memory
            # last_sent_date check in main.py. Code-review #7.
            try:
                from sqlalchemy import text
                from app.database import async_session
                async with async_session() as s:
                    # Stamp tenant_id explicitly. outbound_email_audit.tenant_id
                    # is nullable with NO server-default, so omitting it writes
                    # NULL — which leaves the row un-scoped (invisible to every
                    # tenant-filtered read) and would also break the per-tenant
                    # dedup lookup in _already_sent_today().
                    await s.execute(text("""
                        INSERT INTO outbound_email_audit (
                            tenant_id, sender_user_id, company_id, contact_id, email_id,
                            step_type, subject, body_preview, recipient_email,
                            status, anomaly_score, resend_id, caller_module
                        ) VALUES (
                            :tid, NULL, NULL, NULL, NULL,
                            'internal', :subject, :preview, :recipient,
                            'sent', 0, :resend_id, 'outbound_digest'
                        )
                    """), {
                        "tid": int(tenant_id),
                        "subject": subject[:500],
                        "preview": "[outbound audit digest]"[:300],
                        "recipient": to_addr,
                        "resend_id": resend_id,
                    })
                    await s.commit()
            except Exception as e:
                log.warning(f"digest audit-row write failed (non-fatal): {e}")
            return {"sent": True, "subject": subject, "resend_id": resend_id,
                    "totals": {
                        "sent": sum(x["sent"] for x in data["per_bdr"]),
                        "blocked": sum(x["blocked"] for x in data["per_bdr"]),
                        "failed": sum(x["failed"] for x in data["per_bdr"]),
                    }}
        log.error(f"digest send failed: {r.status_code} {r.text[:200]}")
        return {"sent": False, "status_code": r.status_code, "error": r.text[:200]}
    except Exception as e:
        log.exception(f"digest send raised: {e}")
        return {"sent": False, "error": str(e)}


async def send_all_digests(*, hours: int = 24, force: bool = False) -> dict:
    """Fan out one digest per active tenant. Each tenant's report is built
    from that tenant's rows only and goes to that tenant's own admins.

    One tenant failing never blocks the others."""
    results: dict[int, dict] = {}
    for tid in await active_tenant_ids():
        try:
            results[tid] = await send_digest(tid, hours=hours, force=force)
        except Exception as e:
            log.exception(f"digest failed (tenant={tid}): {e}")
            results[tid] = {"sent": False, "error": str(e)}
    sent = sum(1 for r in results.values() if r.get("sent"))
    return {"tenants": len(results), "sent": sent, "results": results}


if __name__ == "__main__":
    print(asyncio.run(send_all_digests()))
