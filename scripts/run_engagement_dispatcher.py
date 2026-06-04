"""Cron entrypoint for the engagement engine action dispatcher.

Runs ONE dispatcher tick then exits. Designed to be invoked by cron
every 30 seconds:

    * * * * * (cd /opt/backyard-leads && for i in 0 1; do \
                  python -m scripts.run_engagement_dispatcher >> /var/log/eed.log 2>&1 \
                  ; sleep 30 ; done)

Or wired into the existing scheduler.

Environment flags:
    ENGAGEMENT_DISPATCHER_ENABLED  — if not 'true', exits with code 0
                                      without doing anything (kill switch)
    ENGAGEMENT_DISPATCHER_DRY_RUN  — if 'true', runs the full pipeline but
                                      skips actual channel.send() calls

Exit codes:
    0  — tick succeeded (or kill switch off — no work attempted)
    1  — tick had errors (see logs)
"""
from __future__ import annotations
import asyncio
import logging
import os
import sys
import json

from app.engagement_engine.dispatcher import run_dispatcher_tick


def _setup_logging():
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )


async def main() -> int:
    _setup_logging()
    log = logging.getLogger("engagement_engine.dispatcher.runner")

    if os.environ.get("ENGAGEMENT_DISPATCHER_ENABLED", "").lower() != "true":
        log.info("ENGAGEMENT_DISPATCHER_ENABLED != 'true', exiting")
        return 0

    dry_run = os.environ.get("ENGAGEMENT_DISPATCHER_DRY_RUN", "").lower() == "true"
    if dry_run:
        log.info("DRY RUN mode — channel.send() will be skipped")

    report = await run_dispatcher_tick(dry_run=dry_run)

    summary = {
        "duration_ms": report.duration_ms,
        "fetched": report.fetched,
        "sent": report.sent,
        "failed": report.failed,
        "blocked": report.blocked,
        "skipped_stale": report.skipped_stale,
        "skipped_no_adapter": report.skipped_no_adapter,
        "transient_rescheduled": report.transient_rescheduled,
        "out_of_send_window_rescheduled": report.out_of_send_window_rescheduled,
        "error_count": len(report.errors),
    }
    log.info("dispatcher tick: %s", json.dumps(summary))

    if report.errors:
        for err in report.errors:
            log.error("dispatcher error: %s", err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
