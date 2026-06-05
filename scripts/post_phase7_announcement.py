"""Post the Phase 7 cutover announcement to every active BDR's notification
feed so they see it the next time they poll (within 30-60s of login).

The announcement piggybacks on the existing activity-based notification
system (app/routes/notification_routes.py) — each user gets one Activity
row with activity_type='system_announcement', which the Chrome extension
+ web UI surface as a notification toast.

Idempotent: re-running checks for an existing row per user+key and skips.

Run via:
    python -m scripts.post_phase7_announcement
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from app.database import async_session

log = logging.getLogger("phase7.announcement")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


ANNOUNCEMENT_KEY = "phase7_cutover_2026_06_04"

ANNOUNCEMENT_TITLE = "🚀 New engagement engine is now live"

ANNOUNCEMENT_BODY = """\
Hey team — heads up about an update that went live overnight while you were off.

WHAT HAPPENED:
We migrated our outreach engine to a new AI-driven system that AAMP built.
518 of our active contacts have been moved to the new engine. The rest are
still on the legacy engine (they had no pending sends at cutover time).

WHAT'S THE SAME (your day looks normal):
- Your CRM is unchanged
- All your leads, companies, deals, and pipeline view are unchanged
- Today's 178 scheduled emails are still going out, on schedule
- Same Resend inbox, same Twilio numbers, same dialer

WHAT'S NEW:
1. Two new surfaces you should know about (UI coming soon):
   - Approval Queue: when the AI drafts a high-stakes message it needs your
     sign-off. /api/engagement/inbound-unattributed
   - Signal Feed: real-time alerts when something happens at a prospect's
     company (new GMB review, expansion, hiring). /api/engagement/signals/feed
2. The new engine enforces TCPA quiet hours on SMS — no sends 9pm-8am local
3. AI-personalization is dormant for now; emails are going out with their
   existing content

WHAT YOU SHOULD DO:
- Work normally today
- If anything looks off — an email that didn't sound right, a contact you
  expected to get a message who didn't, anything weird — Slack Steve
  IMMEDIATELY. We can flip any single contact back to the old engine in
  seconds.

The dispatcher started processing actions just after midnight Eastern.
First test sends went through cleanly. We'll be watching all morning.

— Steve + the engagement engine
"""


async def main() -> int:
    log.info("Phase 7 announcement: identifying active BDR users")
    async with async_session() as s:
        # Find all active users we should announce to. The User model has
        # `is_active` (default True) — we target active users only.
        rows = await s.execute(text("""
            SELECT id, tenant_id, first_name, last_name, email
            FROM users
            WHERE COALESCE(is_active, TRUE) = TRUE
            ORDER BY id
        """))
        users = list(rows)
        log.info("found %d active users", len(users))

        posted = 0
        skipped = 0
        for u in users:
            # Idempotency: check if an announcement Activity for this key+user
            # already exists.
            check = await s.execute(text("""
                SELECT 1 FROM activities
                WHERE user_id = :u
                  AND activity_type = 'system_announcement'
                  AND metadata_json LIKE :key
                LIMIT 1
            """), {"u": u.id, "key": f'%"key":"{ANNOUNCEMENT_KEY}"%'})
            if check.first() is not None:
                skipped += 1
                continue

            # Build the announcement row. content carries the body; metadata
            # carries the structured key + severity + title so the front-end
            # can render it specially when ready.
            content = f"{ANNOUNCEMENT_TITLE}\n\n{ANNOUNCEMENT_BODY}"
            metadata = (
                f'{{"key": "{ANNOUNCEMENT_KEY}", '
                f'"severity": "info", '
                f'"title": "{ANNOUNCEMENT_TITLE}", '
                f'"requires_ack": true}}'
            )
            await s.execute(text("""
                INSERT INTO activities (
                    tenant_id, user_id, activity_type, content,
                    metadata_json, created_at
                )
                VALUES (
                    :t, :u, 'system_announcement', :content,
                    :meta, NOW()
                )
            """), {
                "t": u.tenant_id, "u": u.id,
                "content": content, "meta": metadata,
            })
            posted += 1
            name = f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email
            log.info("  posted to user %s (%s)", u.id, name)

        await s.commit()

    log.info(
        "Announcement complete: %d posted, %d already had it (skipped)",
        posted, skipped,
    )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))
