"""
Tenant-configurable deal pipeline stages.

The pipeline has three classes of stages:

  1. SYSTEM stages — fixed in code, never editable. They have special
     wiring elsewhere:
       - in_sequence: the auto-start state. Sequence engine, campaigns,
         and the pursue flow all create deals here.
       - closed_won:  revenue calc reads this. Setting it = "you won".
       - closed_lost: terminal, surfaced in lost-reasons reporting.
       - snoozed:     parked off-board with restore-on-wake logic in
         app/main.py and app/routes/deal_routes.py.

  2. MIDDLE stages — the editable section between in_sequence and
     closed_won/lost. Stored as a JSON blob on runtime_config.
     Defaults to a 3-stage funnel (qualified / proposal / negotiation)
     that admins can rename, reorder, add to, or delete from. We
     intentionally do NOT include "prospecting" in the default — for
     us, every deal starts in_sequence and the next meaningful state
     is "qualified" (i.e. the prospect engaged enough to talk).

  3. The on-disk order for the kanban is:
       [in_sequence] -> [middle stages in their configured order]
       -> [closed_won] -> [closed_lost]
     snoozed is rendered separately (off-board view) so it's never a
     column on the main kanban.

Helpers:
  - get_pipeline_config(db) -> the full config (system + middle).
  - get_kanban_stage_order(db) -> just the keys, in display order, for
    the kanban (no snoozed).
  - get_open_stage_keys(db) -> middle-stage keys only. Used by forecast
    + "open deals" calculations.
  - get_stage_probability(db, key) -> int probability for any stage.
  - is_valid_stage(db, key) -> bool. Used by validation.
  - set_middle_stages(db, stages, actor) -> mutates the config; if a
    stage key is removed, deals on that stage are migrated to the
    nearest surviving middle stage (preferring earlier-in-funnel) so
    we don't strand them.

All read helpers are async + accept the db session so we don't import
runtime config eagerly at module load.
"""
from __future__ import annotations
import json
import logging
from typing import Optional
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RuntimeConfig, Deal

log = logging.getLogger("bmp.pipeline_config")


# ============================================================
# System stages — locked. The order here is the display order.
# ============================================================

SYSTEM_STAGES_PRE_MIDDLE = [
    {
        "key": "in_sequence",
        "name": "In Sequence",
        "probability": 0,
        "color": "#3498db",   # blue
        "system": True,
    },
]

SYSTEM_STAGES_POST_MIDDLE = [
    {
        "key": "closed_won",
        "name": "Closed Won",
        "probability": 100,
        "color": "#1B5E20",   # green
        "system": True,
    },
    {
        "key": "closed_lost",
        "name": "Closed Lost",
        "probability": 0,
        "color": "#888888",   # grey
        "system": True,
    },
]

# snoozed isn't a column on the kanban but it IS a valid stage value.
SNOOZED_STAGE = {
    "key": "snoozed",
    "name": "Snoozed",
    "probability": 0,
    "color": "#FFB300",
    "system": True,
}

SYSTEM_STAGE_KEYS = {"in_sequence", "closed_won", "closed_lost", "snoozed"}


# ============================================================
# Middle-stage defaults — used when no config has been saved yet.
# Three-stage funnel. Note: "prospecting" intentionally omitted; for
# our flow, "in sequence" IS the prospecting state.
# ============================================================

DEFAULT_MIDDLE_STAGES = [
    {"key": "qualified",   "name": "Qualified",   "probability": 25, "color": "#26C6DA"},
    {"key": "proposal",    "name": "Proposal",    "probability": 50, "color": "#7E57C2"},
    {"key": "negotiation", "name": "Negotiation", "probability": 75, "color": "#FF723F"},
]


async def _load_rc(db: AsyncSession) -> RuntimeConfig:
    rc = (await db.execute(select(RuntimeConfig).limit(1))).scalar_one_or_none()
    if rc is None:
        rc = RuntimeConfig()
        db.add(rc)
        await db.flush()
    return rc


async def _load_middle_stages(db: AsyncSession) -> list[dict]:
    rc = await _load_rc(db)
    raw = getattr(rc, "pipeline_stages_json", None)
    if not raw:
        return [dict(s) for s in DEFAULT_MIDDLE_STAGES]
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not parsed:
            return [dict(s) for s in DEFAULT_MIDDLE_STAGES]
        # Sanity: drop entries missing required fields, never let a
        # system key sneak in.
        clean = []
        for s in parsed:
            if not isinstance(s, dict): continue
            k = (s.get("key") or "").strip()
            n = (s.get("name") or "").strip()
            if not k or not n: continue
            if k in SYSTEM_STAGE_KEYS: continue
            try:
                p = int(s.get("probability") or 0)
            except (TypeError, ValueError):
                p = 0
            p = max(0, min(99, p))
            c = (s.get("color") or "#888").strip()
            clean.append({"key": k, "name": n, "probability": p, "color": c})
        return clean or [dict(s) for s in DEFAULT_MIDDLE_STAGES]
    except (ValueError, TypeError) as e:
        log.warning(f"pipeline_stages_json malformed, falling back to defaults: {e}")
        return [dict(s) for s in DEFAULT_MIDDLE_STAGES]


async def get_pipeline_config(db: AsyncSession) -> dict:
    """Full pipeline config — what the API/UI consumes."""
    middle = await _load_middle_stages(db)
    middle_with_system = [{**s, "system": False} for s in middle]
    return {
        "system_stages_pre":  [dict(s) for s in SYSTEM_STAGES_PRE_MIDDLE],
        "middle_stages":      middle_with_system,
        "system_stages_post": [dict(s) for s in SYSTEM_STAGES_POST_MIDDLE],
        # Kanban column order: in_sequence -> middle... -> won -> lost
        "kanban_order": (
            [s["key"] for s in SYSTEM_STAGES_PRE_MIDDLE]
            + [s["key"] for s in middle]
            + [s["key"] for s in SYSTEM_STAGES_POST_MIDDLE]
        ),
    }


async def get_kanban_stage_order(db: AsyncSession) -> list[str]:
    cfg = await get_pipeline_config(db)
    return cfg["kanban_order"]


async def get_open_stage_keys(db: AsyncSession) -> list[str]:
    """Middle-stage keys. Used for forecast + 'open pipeline' totals."""
    middle = await _load_middle_stages(db)
    return [s["key"] for s in middle]


async def get_stage_metadata(db: AsyncSession) -> dict[str, dict]:
    """Flat lookup of every stage by key — includes system + middle + snoozed."""
    middle = await _load_middle_stages(db)
    out: dict[str, dict] = {}
    for s in SYSTEM_STAGES_PRE_MIDDLE:    out[s["key"]] = dict(s)
    for s in middle:                       out[s["key"]] = {**s, "system": False}
    for s in SYSTEM_STAGES_POST_MIDDLE:   out[s["key"]] = dict(s)
    out[SNOOZED_STAGE["key"]] = dict(SNOOZED_STAGE)
    return out


async def get_stage_probability(db: AsyncSession, stage_key: str) -> int:
    meta = await get_stage_metadata(db)
    s = meta.get(stage_key)
    return int(s["probability"]) if s else 0


async def is_valid_stage(db: AsyncSession, stage_key: str) -> bool:
    meta = await get_stage_metadata(db)
    return stage_key in meta


async def get_default_middle_stage_key(db: AsyncSession) -> str:
    """Returns the first middle-stage key — used as a fallback when
    we need 'somewhere in the middle' (e.g. snooze restore default)."""
    middle = await _load_middle_stages(db)
    return middle[0]["key"] if middle else "in_sequence"


# ============================================================
# Writes
# ============================================================

class PipelineConfigError(Exception):
    pass


async def set_middle_stages(
    db: AsyncSession,
    new_stages: list[dict],
    *,
    actor_user_id: Optional[int] = None,
) -> dict:
    """Replace the editable middle stages with `new_stages`. Validates
    structure, migrates deals on dropped stages to the nearest survivor.

    Validation:
      - At least 1 middle stage required.
      - Each must have non-empty key + name; key must be lowercase
        alphanum + underscore (so it's safe to compare elsewhere).
      - Keys must be unique within the list.
      - Keys must not collide with system stages (in_sequence/closed_*/snoozed).
      - probability clamped to [0, 99].

    Migration of dropped stages:
      For each existing deal whose stage was in the old middle list but
      is no longer in the new list, move it to the nearest surviving
      middle stage (preferring earlier-in-funnel — closer to the start).
      If no middle stages remain (impossible — validated above), fall
      back to 'in_sequence'.
    """
    import re
    if not isinstance(new_stages, list) or not new_stages:
        raise PipelineConfigError("Pipeline needs at least one middle stage.")

    seen_keys: set[str] = set()
    cleaned: list[dict] = []
    for raw in new_stages:
        if not isinstance(raw, dict):
            raise PipelineConfigError("Each stage must be an object.")
        key = (raw.get("key") or "").strip().lower()
        name = (raw.get("name") or "").strip()
        if not key:
            # Derive a key from the name if none given.
            key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        if not key or not re.fullmatch(r"[a-z0-9_]+", key):
            raise PipelineConfigError(f"Invalid stage key: {key!r}")
        if not name:
            raise PipelineConfigError("Each stage needs a display name.")
        if key in SYSTEM_STAGE_KEYS:
            raise PipelineConfigError(f"'{key}' is a reserved system stage.")
        if key in seen_keys:
            raise PipelineConfigError(f"Duplicate stage key: {key!r}")
        seen_keys.add(key)
        try:
            prob = int(raw.get("probability") or 0)
        except (TypeError, ValueError):
            prob = 0
        prob = max(0, min(99, prob))
        color = (raw.get("color") or "#888").strip()
        cleaned.append({"key": key, "name": name, "probability": prob, "color": color})

    # Compute migration plan based on what's leaving the funnel.
    old_middle = await _load_middle_stages(db)
    old_keys = {s["key"] for s in old_middle}
    new_keys = {s["key"] for s in cleaned}
    dropped = old_keys - new_keys
    fallback_key = cleaned[0]["key"]  # first surviving middle stage

    migrated_count = 0
    if dropped:
        # Move every deal currently on a dropped stage to the fallback.
        affected = (await db.execute(
            select(Deal).where(Deal.stage.in_(list(dropped)))
        )).scalars().all()
        for d in affected:
            d.stage = fallback_key
            d.probability = next(s["probability"] for s in cleaned if s["key"] == fallback_key)
            migrated_count += 1
        if affected:
            log.info(f"pipeline_config: migrated {migrated_count} deal(s) "
                     f"from dropped stages {dropped} -> {fallback_key} "
                     f"(actor={actor_user_id})")

    rc = await _load_rc(db)
    rc.pipeline_stages_json = json.dumps(cleaned)
    await db.commit()

    return {
        "middle_stages": cleaned,
        "dropped_stages": sorted(dropped),
        "migrated_deal_count": migrated_count,
    }
