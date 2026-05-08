"""
Add campaign_targets + campaign_runs tables for God Mode.

CampaignTarget: one row per (vertical, location) pair under a Campaign,
with its own scrape cursor, weight, status, and counters. Replaces the
single current_location_index / current_business_type_index pointer.

CampaignRun: one row per cron tick — drives the morning brief.

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


CREATE_TARGETS = """
CREATE TABLE IF NOT EXISTS campaign_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    vertical VARCHAR(255) NOT NULL,
    location VARCHAR(255) NOT NULL,
    weight INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    contacts_enrolled INTEGER NOT NULL DEFAULT 0,
    sends_made INTEGER NOT NULL DEFAULT 0,
    replies_received INTEGER NOT NULL DEFAULT 0,
    credits_spent FLOAT NOT NULL DEFAULT 0.0,
    enrolled_today INTEGER NOT NULL DEFAULT 0,
    last_daily_reset DATETIME,
    scrape_cursor INTEGER NOT NULL DEFAULT 0,
    last_run_at DATETIME,
    consecutive_empty_runs INTEGER NOT NULL DEFAULT 0,
    paused_reason VARCHAR(255),
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""

CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS campaign_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    started_at DATETIME NOT NULL,
    finished_at DATETIME,
    targets_processed INTEGER NOT NULL DEFAULT 0,
    contacts_enrolled INTEGER NOT NULL DEFAULT 0,
    sends_made INTEGER NOT NULL DEFAULT 0,
    replies_received INTEGER NOT NULL DEFAULT 0,
    credits_spent FLOAT NOT NULL DEFAULT 0.0,
    error TEXT,
    summary_json TEXT
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_campaign_targets_campaign_id ON campaign_targets(campaign_id)",
    "CREATE INDEX IF NOT EXISTS ix_campaign_targets_status ON campaign_targets(status)",
    # Composite uniqueness: a single (campaign, vertical, location) pair only exists once
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_campaign_targets_pair ON campaign_targets(campaign_id, vertical, location)",
    "CREATE INDEX IF NOT EXISTS ix_campaign_runs_campaign_id ON campaign_runs(campaign_id)",
    "CREATE INDEX IF NOT EXISTS ix_campaign_runs_started_at ON campaign_runs(started_at)",
]


async def main() -> None:
    async with engine.begin() as conn:
        targets_existed = (await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='campaign_targets'")
        )).scalar_one_or_none()
        runs_existed = (await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='campaign_runs'")
        )).scalar_one_or_none()

        await conn.execute(text(CREATE_TARGETS))
        await conn.execute(text(CREATE_RUNS))

        if not targets_existed:
            print("+ created campaign_targets table")
        if not runs_existed:
            print("+ created campaign_runs table")

        for idx_sql in INDEXES:
            await conn.execute(text(idx_sql))

        # Backfill: seed CampaignTarget rows for any existing campaigns
        # so God Mode picks up where Autopilot left off. Uses INSERT OR
        # IGNORE against the unique index for idempotency.
        rows = (await conn.execute(text(
            "SELECT id, business_types, locations FROM campaigns"
        ))).fetchall()
        import json as _json
        from datetime import datetime, timezone as _tz
        now_iso = datetime.now(_tz.utc).isoformat()
        seeded = 0
        for camp_id, bts_json, locs_json in rows:
            try:
                bts = _json.loads(bts_json) if bts_json else []
                locs = _json.loads(locs_json) if locs_json else []
            except Exception:
                continue
            for bt in bts:
                for loc in locs:
                    res = await conn.execute(
                        text(
                            "INSERT OR IGNORE INTO campaign_targets "
                            "(campaign_id, vertical, location, weight, status, "
                            " contacts_enrolled, sends_made, replies_received, credits_spent, "
                            " enrolled_today, scrape_cursor, consecutive_empty_runs, "
                            " created_at, updated_at) "
                            "VALUES (:cid, :v, :l, 1, 'active', 0, 0, 0, 0.0, 0, 0, 0, :ts, :ts)"
                        ),
                        {"cid": camp_id, "v": bt, "l": loc, "ts": now_iso},
                    )
                    if res.rowcount:
                        seeded += 1
        if seeded:
            print(f"+ backfilled {seeded} CampaignTarget rows from existing campaigns")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
