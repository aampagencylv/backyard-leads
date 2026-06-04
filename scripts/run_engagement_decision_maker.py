"""Cron entrypoint for the engagement engine decision maker.

Runs ONE tick then exits. Designed to be invoked every 1 minute:

    * * * * * (cd /opt/backyard-leads && \
                  python -m scripts.run_engagement_decision_maker \
                  >> /var/log/eed-decisions.log 2>&1)

Environment flags:
    ENGAGEMENT_DECISION_MAKER_ENABLED — if not 'true', exits 0 (kill switch)

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

from app.engagement_engine.decision_maker import run_decision_maker_tick


def _setup_logging():
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )


async def main() -> int:
    _setup_logging()
    log = logging.getLogger("engagement_engine.decision_maker.runner")

    if os.environ.get("ENGAGEMENT_DECISION_MAKER_ENABLED", "").lower() != "true":
        log.info("ENGAGEMENT_DECISION_MAKER_ENABLED != 'true', exiting")
        return 0

    report = await run_decision_maker_tick()

    summary = {
        "duration_ms": report.duration_ms,
        "signals_scored": report.signals_scored,
        "signals_reacted_to": report.signals_reacted_to,
        "actions_created": report.actions_created,
        "actions_blocked_by_validator": report.actions_blocked_by_validator,
        "cost_budget_exceeded": report.cost_budget_exceeded,
        "parse_failures": report.parse_failures,
        "provider_failures": report.provider_failures,
        "total_cost_usd": round(report.total_cost_usd, 5),
        "error_count": len(report.errors),
    }
    log.info("decision_maker tick: %s", json.dumps(summary))

    if report.errors:
        for err in report.errors:
            log.error("decision_maker error: %s", err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
