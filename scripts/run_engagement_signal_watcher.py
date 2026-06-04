"""Cron entrypoint for the engagement engine signal watcher.

Runs ONE tick then exits. Designed to be invoked every 5 minutes:

    */5 * * * * (cd /opt/backyard-leads && \
                  python -m scripts.run_engagement_signal_watcher \
                  >> /var/log/eed-watcher.log 2>&1)

Or wired into the existing scheduler.

Environment flags:
    ENGAGEMENT_WATCHER_ENABLED  — if not 'true', exits with code 0
                                   without doing anything (kill switch)

Exit codes:
    0  — tick succeeded (or kill switch off)
    1  — tick had errors
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import sys

# Cron runs commands without sourcing systemd's EnvironmentFile.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app.engagement_engine.signal_watcher import run_signal_watcher_tick


def _setup_logging():
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )


async def main() -> int:
    _setup_logging()
    log = logging.getLogger("engagement_engine.signal_watcher.runner")

    if os.environ.get("ENGAGEMENT_WATCHER_ENABLED", "").lower() != "true":
        log.info("ENGAGEMENT_WATCHER_ENABLED != 'true', exiting")
        return 0

    report = await run_signal_watcher_tick()

    summary = {
        "duration_ms": report.duration_ms,
        "fetched": report.fetched,
        "unchanged": report.unchanged,
        "changed_with_signals": report.changed_with_signals,
        "changed_no_signals": report.changed_no_signals,
        "failed": report.failed,
        "deactivated": report.deactivated,
        "signals_written": report.signals_written,
        "no_adapter": report.no_adapter,
        "error_count": len(report.errors),
    }
    log.info("signal_watcher tick: %s", json.dumps(summary))

    if report.errors:
        for err in report.errors:
            log.error("signal_watcher error: %s", err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
