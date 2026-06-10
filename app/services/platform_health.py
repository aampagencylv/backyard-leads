"""Platform health watchdog — surfaces silent failures to humans.

Every production incident in the 2026-06-10 audit had the same shape: a
subsystem failed quietly (Netrows contact discovery down for 6 days,
recording webhook racing Activity creation, Resend 429s marked as sent,
a moderate-mode campaign that never ticks) and nobody knew until the
team noticed missing data days later. This module runs cheap SQL probes
over the last 24h and, when something trips, posts ONE
`system_announcement` Activity to each admin so the problem shows up in
the notification feed the team already reads.

Wired into the main background tick (hourly probe, at most one alert per
ALERT_COOLDOWN_HOURS so a persistent issue doesn't spam the feed).
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("bmp.platform_health")

ALERT_COOLDOWN_HOURS = 12
ALERT_KEY = "platform_health_alert"


async def run_platform_health_check(db: AsyncSession) -> dict:
    """Run all probes; post an admin notification if any trip.
    Returns {"issues": [...], "alerted": bool} for logging/tests."""
    issues: list[str] = []

    # 1. Phantom sends — actions marked sent with no provider id. Post-fix
    #    this must be zero; any recurrence means the send-result check
    #    regressed or a new channel skips external_id.
    n = (await db.execute(text("""
        SELECT COUNT(*) FROM actions a
        JOIN channel_types ct ON ct.id = a.channel_id
        WHERE a.status = 'sent' AND a.external_id IS NULL
          AND ct.code IN ('email', 'sms')
          AND a.executed_at > NOW() - INTERVAL '24 hours'
    """))).scalar() or 0
    if n > 0:
        issues.append(f"{n} email/SMS action(s) marked sent with no provider id "
                      f"(phantom sends) in the last 24h")

    # 2. Dispatcher liveness — scheduled engine actions overdue by 30+ min
    #    mean the cron is dead, disabled, or erroring every tick.
    n = (await db.execute(text("""
        SELECT COUNT(*) FROM actions a
        JOIN channel_types ct ON ct.id = a.channel_id
        JOIN engagements e ON e.id = a.engagement_id
        WHERE a.status = 'scheduled' AND ct.code IN ('email', 'sms')
          AND e.status = 'active'
          AND a.scheduled_at < NOW() - INTERVAL '30 minutes'
    """))).scalar() or 0
    if n > 5:
        issues.append(f"{n} auto-send steps are 30+ minutes overdue — "
                      f"the engagement dispatcher may not be running")

    # 3. Contact-discovery health — meaningful intake with ~no emails found
    #    is the signature of the Netrows outage that emptied Naples/Houston.
    row = (await db.execute(text("""
        SELECT COUNT(*) AS n,
               COUNT(*) FILTER (WHERE EXISTS (
                 SELECT 1 FROM contacts ct WHERE ct.company_id = co.id
                   AND ct.email IS NOT NULL AND ct.email != '')) AS with_email
        FROM companies co
        WHERE co.created_at > NOW() - INTERVAL '24 hours' AND co.enriched
    """))).first()
    if row and row.n >= 20 and row.with_email * 20 < row.n:  # <5% discovery
        issues.append(f"contact discovery found emails for only {row.with_email} "
                      f"of {row.n} companies enriched in the last 24h — "
                      f"Netrows/Hunter may be down or out of quota")

    # 4. Campaign errors — now that lookup failures are logged instead of
    #    swallowed, a burst of error rows is the early-warning signal.
    n = (await db.execute(text("""
        SELECT COUNT(*) FROM campaign_logs
        WHERE action = 'error' AND created_at > NOW() - INTERVAL '24 hours'
    """))).scalar() or 0
    if n > 25:
        issues.append(f"{n} campaign error log entries in the last 24h "
                      f"(search/enrichment/contact-lookup failures)")

    # 5. Failed sends — permanent channel failures.
    n = (await db.execute(text("""
        SELECT COUNT(*) FROM actions
        WHERE status = 'failed' AND updated_at > NOW() - INTERVAL '24 hours'
    """))).scalar() or 0
    if n > 5:
        issues.append(f"{n} engine actions hard-failed in the last 24h")

    # 6. Recordings — connected calls older than 3h with no recording mean
    #    the webhook AND the backfill sweep both missed them.
    n = (await db.execute(text("""
        SELECT COUNT(*) FROM activities
        WHERE activity_type = 'call'
          AND created_at BETWEEN NOW() - INTERVAL '24 hours' AND NOW() - INTERVAL '3 hours'
          AND twilio_call_sid IS NOT NULL AND recording_url IS NULL
          AND (content LIKE '%connected%' OR content LIKE '%gatekeeper%')
    """))).scalar() or 0
    if n > 5:
        issues.append(f"{n} connected calls from the last 24h still have no "
                      f"recording attached")

    if not issues:
        return {"issues": [], "alerted": False}

    # Cooldown: at most one alert per window, so a persistent issue doesn't
    # bury the feed.
    recent = (await db.execute(text("""
        SELECT 1 FROM activities
        WHERE activity_type = 'system_announcement'
          AND metadata_json LIKE :key
          AND created_at > NOW() - make_interval(hours => :cool)
        LIMIT 1
    """), {"key": f'%"key": "{ALERT_KEY}"%', "cool": ALERT_COOLDOWN_HOURS})).first()
    if recent is not None:
        log.warning("platform health issues (alert suppressed by cooldown): %s", issues)
        return {"issues": issues, "alerted": False}

    title = "⚠️ Platform health check found issues"
    body = "\n".join(f"• {i}" for i in issues)
    admins = (await db.execute(text("""
        SELECT id, tenant_id FROM users
        WHERE role IN ('admin', 'super_admin') AND COALESCE(is_active, TRUE) = TRUE
    """))).fetchall()
    for u in admins:
        await db.execute(text("""
            INSERT INTO activities (
                tenant_id, user_id, activity_type, content, metadata_json, created_at
            ) VALUES (:t, :u, 'system_announcement', :content, :meta, NOW())
        """), {
            "t": u.tenant_id, "u": u.id,
            "content": f"{title}\n\n{body}",
            "meta": json.dumps({"key": ALERT_KEY, "severity": "warning",
                                "title": title}),
        })
    await db.commit()
    log.warning("platform health alert posted to %d admins: %s", len(admins), issues)
    return {"issues": issues, "alerted": True}
