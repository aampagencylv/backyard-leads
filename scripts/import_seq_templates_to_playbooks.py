"""Backfill: convert legacy seq_templates rows into engagement-engine playbooks.

The old sequence engine stored "templates" with steps in `seq_templates` +
`seq_template_steps`. The new engine has equivalent shape in `playbooks` +
`playbook_actions`, with a `legacy_seq_template_id` field linking back.

Run via:
    python -m scripts.import_seq_templates_to_playbooks

Idempotent — running it twice does NOT create duplicate playbooks. A row
in `playbooks` with `legacy_seq_template_id = X` means seq_template X has
already been imported.

Each seq_template becomes a `linear_sequence` mode playbook in
`cold_outreach` phase (the only phase seq_templates ever represented).
Each seq_template_step becomes a playbook_action with the same channel +
day_offset + content.

This script does NOT:
  - Activate any engagements (Phase 7 handles cutover)
  - Modify or deactivate any seq_templates rows (the old engine keeps using them)
  - Touch the engagements table

Phase 7's cutover script picks up where this leaves off — enrolling new
prospects on the imported playbooks, marking the corresponding
seq_enrollments to drain, etc.
"""
from __future__ import annotations
import asyncio
import logging
import sys

from sqlalchemy import text

from app.database import engine

log = logging.getLogger("import_seq_templates")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")


async def main() -> int:
    imported = 0
    skipped = 0
    errored = 0

    # Read pending templates first (separate read-only connection)
    async with engine.begin() as conn:
        rows = await conn.execute(text("""
            SELECT st.id, st.tenant_id, st.name, st.description,
                   st.is_default, st.is_active, st.created_by_user_id
            FROM seq_templates st
            WHERE NOT EXISTS (
                SELECT 1 FROM playbooks p
                WHERE p.legacy_seq_template_id = st.id
            )
            ORDER BY st.id
        """))
        templates = [r for r in rows]
    log.info("found %d seq_templates pending import", len(templates))

    # Per-template fresh transaction so a failure on one doesn't poison others
    for tmpl in templates:
        try:
            async with engine.begin() as conn:
                new_pb_id = await _import_one_template(conn, tmpl)
                log.info(
                    "imported seq_template %s -> playbook %s (%r)",
                    tmpl.id, new_pb_id, tmpl.name,
                )
                imported += 1
        except Exception as e:
            log.error("failed to import seq_template %s: %s", tmpl.id, e)
            errored += 1

    log.info("import complete: %d imported, %d skipped, %d errored",
             imported, skipped, errored)
    return 1 if errored > 0 else 0


async def _import_one_template(conn, tmpl) -> int:
    """Create a playbook + actions corresponding to one seq_template."""
    # 1) Create the playbook row
    pb_row = await conn.execute(text("""
        INSERT INTO playbooks (
            tenant_id, name, description, phase, mode,
            ai_strategy_json, legacy_seq_template_id,
            is_active, version, created_by_user_id
        )
        VALUES (
            :t, :name, :desc, 'cold_outreach', 'linear_sequence',
            '{}'::jsonb, :legacy_id,
            :active, 1, :user_id
        )
        RETURNING id
    """), {
        "t": tmpl.tenant_id,
        "name": tmpl.name,
        "desc": tmpl.description,
        "legacy_id": tmpl.id,
        "active": tmpl.is_active,
        "user_id": tmpl.created_by_user_id,
    })
    new_pb_id = pb_row.first().id

    # 2) Copy the active steps. Resolve channel codes to ids.
    step_rows = await conn.execute(text("""
        SELECT id, step_order, channel, day_offset_from_enroll,
               step_label, subject_template, body_template,
               skip_conditions_json, auto_execute, is_active
        FROM seq_template_steps
        WHERE template_id = :t AND is_active = TRUE
        ORDER BY step_order
    """), {"t": tmpl.id})
    steps = list(step_rows)

    if not steps:
        return new_pb_id

    # Build channel code → id map (cheap; ~6 rows in channel_types)
    ch_rows = await conn.execute(text(
        "SELECT id, code FROM channel_types WHERE is_active = TRUE"
    ))
    ch_map = {r.code: r.id for r in ch_rows}

    for step in steps:
        channel_id = ch_map.get(step.channel)
        if channel_id is None:
            log.warning(
                "skipping seq_template_step %s: unknown channel %r",
                step.id, step.channel,
            )
            continue

        # Determine trigger: auto_execute=True is the default scheduled
        # behavior; auto_execute=False corresponds to manual approval, which
        # we represent as trigger='scheduled' + ai_personalization_mode='none'
        # for now (the actual approval gate is per-action on the engine side)
        trigger = "scheduled"
        ai_mode = "augmented" if step.channel == "email" else "none"

        # skip_conditions in the legacy schema was a JSON array of strings;
        # in the new schema we use a dict so wrap it.
        skip_json = step.skip_conditions_json or "[]"
        if isinstance(skip_json, str):
            try:
                import json as _json
                parsed = _json.loads(skip_json)
                if isinstance(parsed, list):
                    parsed = {"legacy_conditions": parsed}
                wrapped = _json.dumps(parsed)
            except Exception:
                wrapped = '{"legacy_conditions": []}'
        else:
            wrapped = '{}'

        await conn.execute(text("""
            INSERT INTO playbook_actions (
                playbook_id, tenant_id, action_order, channel_id, trigger,
                trigger_config_json, ai_personalization_mode,
                subject_template, body_template, day_offset,
                skip_conditions_json, legacy_seq_step_id, is_active
            )
            VALUES (
                :pb, :t, :ord, :ch, :trigger,
                '{}'::jsonb, :pmode,
                :subj, :body, :offset,
                CAST(:skip AS jsonb), :legacy_step, :active
            )
        """), {
            "pb": new_pb_id,
            "t": tmpl.tenant_id,
            "ord": step.step_order,
            "ch": channel_id,
            "trigger": trigger,
            "pmode": ai_mode,
            "subj": step.subject_template,
            "body": step.body_template,
            "offset": step.day_offset_from_enroll,
            "skip": wrapped,
            "legacy_step": step.id,
            "active": step.is_active,
        })

    return new_pb_id


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
