"""Playbook editor REST endpoints.

A playbook is a reusable engagement strategy. Modes:
  - linear_sequence  — like the old 30-day sequence (day_offset required)
  - signal_driven    — triggered by signals (day_offset must be NULL)
  - hybrid           — both
  - trigger_response — single-shot reactive

Versioning model:
  - Editing a playbook (or any of its steps) creates a NEW version of the
    playbook with version=N+1 and parent_playbook_id=current.id.
  - The old version row gets is_active=FALSE.
  - Engagements pinned to the old version (via current_playbook_id +
    current_playbook_version) keep running on it — they don't auto-migrate.
  - New enrollments use the active version.

day_offset / mode coupling is enforced by the
`enforce_day_offset_mode_consistency` DB trigger (built in Phase 1). The
API surfaces friendly errors but the trigger is the structural defense.

Test-send doesn't actually dispatch — it renders the templates against a
synthetic contact context so the BDR can see what would go out.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field, ConfigDict

from app.tenancy import get_tenant_db
from app.auth import get_current_user
from app.models import User

log = logging.getLogger("engagement_engine.playbook_routes")

router = APIRouter(
    prefix="/api/engagement",
    tags=["engagement-playbooks"],
)


# ════════════════════════════════════════════════════════════════════════════
# Schemas
# ════════════════════════════════════════════════════════════════════════════

PhaseLit = Literal[
    "cold_outreach", "meeting_set", "post_meeting_nurture",
    "qualified", "customer", "declined", "lost", "dormant", "cross_phase",
]
ModeLit = Literal["linear_sequence", "signal_driven", "hybrid", "trigger_response"]
TriggerLit = Literal[
    "scheduled", "on_signal", "on_no_engagement_for_n_days",
    "on_phase_transition", "on_reply_intent",
]
PersonalizationLit = Literal["none", "augmented", "generated_from_context"]


class PlaybookActionOut(BaseModel):
    id: int
    action_order: int
    channel_code: str
    trigger: str
    trigger_config_json: dict
    ai_personalization_mode: str
    subject_template: Optional[str]
    body_template: Optional[str]
    task_template: Optional[str]
    day_offset: Optional[int]
    skip_conditions_json: dict
    is_active: bool


class PlaybookOut(BaseModel):
    id: int
    name: str
    description: Optional[str]
    phase: str
    mode: str
    duration_max_days: Optional[int]
    ai_strategy_json: dict
    is_active: bool
    version: int
    parent_playbook_id: Optional[int]
    legacy_seq_template_id: Optional[int]
    created_at: datetime
    actions: list[PlaybookActionOut]


class PlaybookCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    phase: PhaseLit
    mode: ModeLit
    duration_max_days: Optional[int] = Field(default=None, gt=0, le=3650)
    ai_strategy_json: dict = Field(default_factory=dict)


class PlaybookUpdate(BaseModel):
    """Editing metadata creates a NEW VERSION of the playbook."""
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    duration_max_days: Optional[int] = Field(default=None, gt=0, le=3650)
    ai_strategy_json: Optional[dict] = None
    # Note: phase + mode are NOT editable post-creation. Changing them is a
    # different playbook conceptually — create a new playbook instead.


class PlaybookActionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel_code: str  # FK lookup, validated at insert time
    trigger: TriggerLit = "scheduled"
    trigger_config_json: dict = Field(default_factory=dict)
    ai_personalization_mode: PersonalizationLit = "augmented"
    subject_template: Optional[str] = Field(default=None, max_length=500)
    body_template: Optional[str] = Field(default=None, max_length=20_000)
    task_template: Optional[str] = Field(default=None, max_length=2000)
    day_offset: Optional[int] = Field(default=None, ge=0, le=3650)
    skip_conditions_json: dict = Field(default_factory=dict)


class PlaybookActionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel_code: Optional[str] = None
    trigger: Optional[TriggerLit] = None
    trigger_config_json: Optional[dict] = None
    ai_personalization_mode: Optional[PersonalizationLit] = None
    subject_template: Optional[str] = Field(default=None, max_length=500)
    body_template: Optional[str] = Field(default=None, max_length=20_000)
    task_template: Optional[str] = Field(default=None, max_length=2000)
    day_offset: Optional[int] = Field(default=None, ge=0, le=3650)
    skip_conditions_json: Optional[dict] = None


class ReorderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_order_index: int = Field(ge=1, le=200)


class TestSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_id: Optional[int] = None  # use a real contact's fields
    sample_contact: Optional[dict] = None  # OR a synthetic dict


class TestSendResponse(BaseModel):
    channel_code: str
    rendered_subject: Optional[str]
    rendered_body: Optional[str]
    rendered_task: Optional[str]
    placeholder_warnings: list[str]


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

async def _resolve_channel_id(db: AsyncSession, code: str) -> int:
    row = await db.execute(text(
        "SELECT id FROM channel_types WHERE code = :c AND is_active = TRUE"
    ), {"c": code})
    r = row.first()
    if r is None:
        raise HTTPException(
            status_code=400, detail=f"unknown channel_code: {code}",
        )
    return r.id


async def _channel_code_for_id(db: AsyncSession, channel_id: int) -> str:
    row = await db.execute(text(
        "SELECT code FROM channel_types WHERE id = :id"
    ), {"id": channel_id})
    r = row.first()
    return r.code if r else "unknown"


async def _load_playbook(db: AsyncSession, playbook_id: int) -> PlaybookOut:
    """Load a playbook + its active actions."""
    _tid = db.info.get("tenant_id")
    pb_row = await db.execute(text("""
        SELECT id, name, description, phase, mode, duration_max_days,
               ai_strategy_json, is_active, version, parent_playbook_id,
               legacy_seq_template_id, created_at
        FROM playbooks WHERE id = :id AND tenant_id = :tid
    """), {"id": playbook_id, "tid": _tid})
    pb = pb_row.first()
    if pb is None:
        raise HTTPException(status_code=404, detail="playbook not found")

    actions_rows = await db.execute(text("""
        SELECT pa.id, pa.action_order, ct.code AS channel_code,
               pa.trigger, pa.trigger_config_json, pa.ai_personalization_mode,
               pa.subject_template, pa.body_template, pa.task_template,
               pa.day_offset, pa.skip_conditions_json, pa.is_active
        FROM playbook_actions pa
        JOIN channel_types ct ON ct.id = pa.channel_id
        WHERE pa.playbook_id = :id AND pa.tenant_id = :tid AND pa.is_active = TRUE
        ORDER BY pa.action_order
    """), {"id": playbook_id, "tid": _tid})
    actions = [
        PlaybookActionOut(
            id=r.id, action_order=r.action_order,
            channel_code=r.channel_code, trigger=r.trigger,
            trigger_config_json=_to_dict(r.trigger_config_json),
            ai_personalization_mode=r.ai_personalization_mode,
            subject_template=r.subject_template,
            body_template=r.body_template,
            task_template=r.task_template,
            day_offset=r.day_offset,
            skip_conditions_json=_to_dict(r.skip_conditions_json),
            is_active=r.is_active,
        ) for r in actions_rows
    ]

    return PlaybookOut(
        id=pb.id, name=pb.name, description=pb.description,
        phase=pb.phase, mode=pb.mode,
        duration_max_days=pb.duration_max_days,
        ai_strategy_json=_to_dict(pb.ai_strategy_json),
        is_active=pb.is_active, version=pb.version,
        parent_playbook_id=pb.parent_playbook_id,
        legacy_seq_template_id=pb.legacy_seq_template_id,
        created_at=pb.created_at,
        actions=actions,
    )


def _to_dict(json_val) -> dict:
    if json_val is None:
        return {}
    if isinstance(json_val, dict):
        return json_val
    if isinstance(json_val, str):
        try:
            return json.loads(json_val)
        except Exception:
            return {}
    return {}


async def _has_active_enrollments(db: AsyncSession, playbook_id: int) -> bool:
    """Are any engagements currently pinned to this playbook id?"""
    row = await db.execute(text("""
        SELECT 1 FROM engagements
        WHERE current_playbook_id = :id
          AND tenant_id = :tid
          AND status NOT IN ('terminal',)
        LIMIT 1
    """), {"id": playbook_id, "tid": db.info.get("tenant_id")})
    return row.first() is not None


async def _create_new_version(
    db: AsyncSession,
    *,
    old_playbook_id: int,
    metadata_updates: dict,
    user_id: int,
) -> int:
    """Create a new playbook version that supersedes old_playbook_id.

    Returns the new playbook's id. Marks the old one is_active=FALSE.
    Clones all active step rows so the new version inherits the schedule;
    callers then mutate the cloned steps for the edit being made.
    """
    # 1) Insert the new playbook row
    new_pb_row = await db.execute(text("""
        INSERT INTO playbooks (
            tenant_id, name, description, phase, mode,
            duration_max_days, ai_strategy_json, legacy_seq_template_id,
            is_active, version, parent_playbook_id, created_by_user_id
        )
        SELECT
            tenant_id,
            COALESCE(:name, name),
            COALESCE(:description, description),
            phase, mode,
            COALESCE(:dur, duration_max_days),
            CAST(COALESCE(:strategy, CAST(ai_strategy_json AS text)) AS jsonb),
            legacy_seq_template_id,
            TRUE, version + 1, id, :user_id
        FROM playbooks WHERE id = :old_id AND tenant_id = :tid
        RETURNING id, version
    """), {
        "old_id": old_playbook_id,
        "tid": db.info.get("tenant_id"),
        "name": metadata_updates.get("name"),
        "description": metadata_updates.get("description"),
        "dur": metadata_updates.get("duration_max_days"),
        "strategy": json.dumps(metadata_updates["ai_strategy_json"])
                    if metadata_updates.get("ai_strategy_json") is not None else None,
        "user_id": user_id,
    })
    new_row = new_pb_row.first()
    if new_row is None:
        raise HTTPException(status_code=404, detail="source playbook not found")
    new_id = new_row.id

    # 2) Clone the active steps
    await db.execute(text("""
        INSERT INTO playbook_actions (
            playbook_id, tenant_id, action_order, channel_id, trigger,
            trigger_config_json, ai_personalization_mode,
            subject_template, body_template, task_template, day_offset,
            skip_conditions_json, legacy_seq_step_id, is_active
        )
        SELECT
            :new_id, tenant_id, action_order, channel_id, trigger,
            trigger_config_json, ai_personalization_mode,
            subject_template, body_template, task_template, day_offset,
            skip_conditions_json, legacy_seq_step_id, is_active
        FROM playbook_actions
        WHERE playbook_id = :old_id AND tenant_id = :tid AND is_active = TRUE
    """), {"new_id": new_id, "old_id": old_playbook_id, "tid": db.info.get("tenant_id")})

    # 3) Mark the old playbook inactive
    await db.execute(text("""
        UPDATE playbooks SET is_active = FALSE, updated_at = NOW()
        WHERE id = :old_id AND tenant_id = :tid
    """), {"old_id": old_playbook_id, "tid": db.info.get("tenant_id")})

    return new_id


def _placeholder_warnings(text_val: str | None) -> list[str]:
    """Find unrendered {{placeholder}} tokens in a rendered string. Used
    by test-send to flag templates that won't actually populate at send
    time."""
    if not text_val:
        return []
    import re
    matches = re.findall(r"\{\{\s*[\w\.\-]+\s*\}\}", text_val)
    if not matches:
        return []
    return sorted(set(matches))


def _render_template(template: str | None, context: dict) -> str | None:
    """Trivial mustache-style render. Replaces {{key}} with context[key].
    Phase 6 minimum — Jinja2 + filters land if/when we need them."""
    if template is None:
        return None
    import re
    def _sub(m):
        key = m.group(1).strip()
        val = context.get(key, "")
        return str(val) if val is not None else ""
    return re.sub(r"\{\{\s*([\w\.\-]+)\s*\}\}", _sub, template)


# ════════════════════════════════════════════════════════════════════════════
# Playbook CRUD
# ════════════════════════════════════════════════════════════════════════════

@router.get("/playbooks", response_model=list[PlaybookOut])
async def list_playbooks(
    phase: Optional[str] = None,
    mode: Optional[str] = None,
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> list[PlaybookOut]:
    """List the tenant's playbooks. By default only returns the currently
    active version of each playbook chain."""
    where = ["tenant_id = :tid"]
    params: dict = {"tid": db.info.get("tenant_id")}
    if not include_inactive:
        where.append("is_active = TRUE")
    if phase:
        where.append("phase = :phase"); params["phase"] = phase
    if mode:
        where.append("mode = :mode"); params["mode"] = mode

    rows = await db.execute(text(f"""
        SELECT id FROM playbooks
        WHERE {' AND '.join(where)}
        ORDER BY phase, name, version DESC
    """), params)
    ids = [r.id for r in rows]
    return [await _load_playbook(db, pid) for pid in ids]


@router.get("/playbooks/{playbook_id}", response_model=PlaybookOut)
async def get_playbook(
    playbook_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> PlaybookOut:
    return await _load_playbook(db, playbook_id)


@router.post("/playbooks", response_model=PlaybookOut, status_code=201)
async def create_playbook(
    body: PlaybookCreate,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> PlaybookOut:
    """Create a new playbook (v1) with no actions yet."""
    row = await db.execute(text("""
        INSERT INTO playbooks (
            tenant_id, name, description, phase, mode,
            duration_max_days, ai_strategy_json, is_active, version,
            created_by_user_id
        )
        VALUES (
            :t, :name, :description, :phase, :mode,
            :dur, CAST(:strategy AS jsonb), TRUE, 1, :user_id
        )
        RETURNING id
    """), {
        "t": current_user.tenant_id,
        "name": body.name,
        "description": body.description,
        "phase": body.phase,
        "mode": body.mode,
        "dur": body.duration_max_days,
        "strategy": json.dumps(body.ai_strategy_json),
        "user_id": current_user.id,
    })
    new_id = row.first().id
    await db.commit()
    log.info("playbook created: id=%s by user=%s tenant=%s",
             new_id, current_user.id, current_user.tenant_id)
    return await _load_playbook(db, new_id)


@router.put("/playbooks/{playbook_id}", response_model=PlaybookOut)
async def update_playbook(
    playbook_id: int,
    body: PlaybookUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> PlaybookOut:
    """Update playbook metadata. If active engagements are pinned to this
    playbook, creates a new version. Otherwise mutates in place."""
    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.description is not None:
        updates["description"] = body.description
    if body.duration_max_days is not None:
        updates["duration_max_days"] = body.duration_max_days
    if body.ai_strategy_json is not None:
        updates["ai_strategy_json"] = body.ai_strategy_json

    if not updates:
        return await _load_playbook(db, playbook_id)

    in_use = await _has_active_enrollments(db, playbook_id)
    if in_use:
        new_id = await _create_new_version(
            db, old_playbook_id=playbook_id,
            metadata_updates=updates, user_id=current_user.id,
        )
        await db.commit()
        log.info("playbook %s edited → new version %s (had active enrollments)",
                 playbook_id, new_id)
        return await _load_playbook(db, new_id)

    # In-place update path
    set_parts = []
    params: dict = {"id": playbook_id}
    for k, v in updates.items():
        if k == "ai_strategy_json":
            set_parts.append(f"{k} = CAST(:{k} AS jsonb)")
            params[k] = json.dumps(v)
        else:
            set_parts.append(f"{k} = :{k}")
            params[k] = v
    set_parts.append("updated_at = NOW()")
    params["tid"] = db.info.get("tenant_id")
    await db.execute(text(f"""
        UPDATE playbooks SET {', '.join(set_parts)} WHERE id = :id AND tenant_id = :tid
    """), params)
    await db.commit()
    return await _load_playbook(db, playbook_id)


@router.delete("/playbooks/{playbook_id}", status_code=204)
async def deactivate_playbook(
    playbook_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    """Soft-delete: marks playbook + its actions inactive. Active enrollments
    keep working (they hold a snapshot via current_playbook_id +
    current_playbook_version). New enrollments cannot use this playbook."""
    result = await db.execute(text("""
        UPDATE playbooks SET is_active = FALSE, updated_at = NOW()
        WHERE id = :id AND tenant_id = :tid
        RETURNING id
    """), {"id": playbook_id, "tid": db.info.get("tenant_id")})
    if result.first() is None:
        raise HTTPException(status_code=404, detail="playbook not found")
    await db.commit()
    log.info("playbook %s deactivated by user=%s", playbook_id, current_user.id)


# ════════════════════════════════════════════════════════════════════════════
# Step CRUD
# ════════════════════════════════════════════════════════════════════════════

@router.post("/playbooks/{playbook_id}/actions",
             response_model=PlaybookActionOut, status_code=201)
async def add_playbook_action(
    playbook_id: int,
    body: PlaybookActionCreate,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> PlaybookActionOut:
    """Append a new step to a playbook. Versions the playbook if it has
    active enrollments."""
    # Version-bump first if in use
    in_use = await _has_active_enrollments(db, playbook_id)
    target_pb_id = playbook_id
    if in_use:
        target_pb_id = await _create_new_version(
            db, old_playbook_id=playbook_id, metadata_updates={},
            user_id=current_user.id,
        )

    channel_id = await _resolve_channel_id(db, body.channel_code)

    # Determine next action_order
    next_order_row = await db.execute(text("""
        SELECT COALESCE(MAX(action_order), 0) + 1 AS next FROM playbook_actions
        WHERE playbook_id = :id AND tenant_id = :tid AND is_active = TRUE
    """), {"id": target_pb_id, "tid": db.info.get("tenant_id")})
    next_order = next_order_row.first().next

    try:
        row = await db.execute(text("""
            INSERT INTO playbook_actions (
                playbook_id, tenant_id, action_order, channel_id, trigger,
                trigger_config_json, ai_personalization_mode,
                subject_template, body_template, task_template, day_offset,
                skip_conditions_json, is_active
            )
            VALUES (
                :pb, :t, :ord, :ch, :trigger,
                CAST(:tc AS jsonb), :pmode,
                :subj, :body, :task, :offset,
                CAST(:skip AS jsonb), TRUE
            )
            RETURNING id, action_order
        """), {
            "pb": target_pb_id,
            "t": current_user.tenant_id,
            "ord": next_order,
            "ch": channel_id,
            "trigger": body.trigger,
            "tc": json.dumps(body.trigger_config_json),
            "pmode": body.ai_personalization_mode,
            "subj": body.subject_template,
            "body": body.body_template,
            "task": body.task_template,
            "offset": body.day_offset,
            "skip": json.dumps(body.skip_conditions_json),
        })
        new_action_id = row.first().id
        await db.commit()
    except Exception as e:
        # The day_offset/mode trigger fires here. Convert the Postgres
        # exception to a friendly 400.
        msg = str(e)
        if "day_offset required" in msg:
            raise HTTPException(
                status_code=400,
                detail="day_offset required for linear_sequence playbook mode",
            )
        if "day_offset must be NULL" in msg:
            raise HTTPException(
                status_code=400,
                detail="day_offset forbidden for signal_driven playbook mode",
            )
        raise

    # Return the created action
    row = await db.execute(text("""
        SELECT pa.id, pa.action_order, ct.code AS channel_code,
               pa.trigger, pa.trigger_config_json, pa.ai_personalization_mode,
               pa.subject_template, pa.body_template, pa.task_template,
               pa.day_offset, pa.skip_conditions_json, pa.is_active
        FROM playbook_actions pa
        JOIN channel_types ct ON ct.id = pa.channel_id
        WHERE pa.id = :id AND pa.tenant_id = :tid
    """), {"id": new_action_id, "tid": db.info.get("tenant_id")})
    r = row.first()
    return PlaybookActionOut(
        id=r.id, action_order=r.action_order, channel_code=r.channel_code,
        trigger=r.trigger,
        trigger_config_json=_to_dict(r.trigger_config_json),
        ai_personalization_mode=r.ai_personalization_mode,
        subject_template=r.subject_template,
        body_template=r.body_template,
        task_template=r.task_template,
        day_offset=r.day_offset,
        skip_conditions_json=_to_dict(r.skip_conditions_json),
        is_active=r.is_active,
    )


@router.put("/playbook-actions/{action_id}",
            response_model=PlaybookActionOut)
async def edit_playbook_action(
    action_id: int,
    body: PlaybookActionUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> PlaybookActionOut:
    """Edit a single step. Versions the parent playbook if needed."""

    # Find the parent playbook
    parent_row = await db.execute(text(
        "SELECT playbook_id FROM playbook_actions WHERE id = :id AND tenant_id = :tid"
    ), {"id": action_id, "tid": db.info.get("tenant_id")})
    parent = parent_row.first()
    if parent is None:
        raise HTTPException(status_code=404, detail="action not found")
    parent_id = parent.playbook_id

    # Version-bump if needed; remap action_id to the cloned step in the new version
    target_action_id = action_id
    if await _has_active_enrollments(db, parent_id):
        # Find this action's order in the source playbook
        order_row = await db.execute(text(
            "SELECT action_order FROM playbook_actions WHERE id = :id AND tenant_id = :tid"
        ), {"id": action_id, "tid": db.info.get("tenant_id")})
        action_order = order_row.first().action_order

        new_pb_id = await _create_new_version(
            db, old_playbook_id=parent_id, metadata_updates={},
            user_id=current_user.id,
        )
        # Find the corresponding cloned action_id in the new playbook
        cloned_row = await db.execute(text("""
            SELECT id FROM playbook_actions
            WHERE playbook_id = :pb AND tenant_id = :tid
              AND action_order = :ord AND is_active = TRUE
        """), {"pb": new_pb_id, "ord": action_order, "tid": db.info.get("tenant_id")})
        target_action_id = cloned_row.first().id

    # Apply the edit
    sets = []
    params: dict = {"id": target_action_id}
    if body.channel_code is not None:
        ch_id = await _resolve_channel_id(db, body.channel_code)
        sets.append("channel_id = :ch"); params["ch"] = ch_id
    if body.trigger is not None:
        sets.append("trigger = :tr"); params["tr"] = body.trigger
    if body.trigger_config_json is not None:
        sets.append("trigger_config_json = CAST(:tc AS jsonb)")
        params["tc"] = json.dumps(body.trigger_config_json)
    if body.ai_personalization_mode is not None:
        sets.append("ai_personalization_mode = :pm")
        params["pm"] = body.ai_personalization_mode
    if body.subject_template is not None:
        sets.append("subject_template = :subj"); params["subj"] = body.subject_template
    if body.body_template is not None:
        sets.append("body_template = :body"); params["body"] = body.body_template
    if body.task_template is not None:
        sets.append("task_template = :task"); params["task"] = body.task_template
    if body.day_offset is not None:
        sets.append("day_offset = :off"); params["off"] = body.day_offset
    if body.skip_conditions_json is not None:
        sets.append("skip_conditions_json = CAST(:skip AS jsonb)")
        params["skip"] = json.dumps(body.skip_conditions_json)

    if not sets:
        # No-op edit
        pass
    else:
        sets.append("updated_at = NOW()")
        params["tid"] = db.info.get("tenant_id")
        try:
            await db.execute(text(
                f"UPDATE playbook_actions SET {', '.join(sets)} WHERE id = :id AND tenant_id = :tid"
            ), params)
        except Exception as e:
            msg = str(e)
            if "day_offset required" in msg:
                raise HTTPException(
                    status_code=400,
                    detail="day_offset required for linear_sequence playbook mode",
                )
            if "day_offset must be NULL" in msg:
                raise HTTPException(
                    status_code=400,
                    detail="day_offset forbidden for signal_driven playbook mode",
                )
            raise
    await db.commit()

    # Return updated action
    row = await db.execute(text("""
        SELECT pa.id, pa.action_order, ct.code AS channel_code,
               pa.trigger, pa.trigger_config_json, pa.ai_personalization_mode,
               pa.subject_template, pa.body_template, pa.task_template,
               pa.day_offset, pa.skip_conditions_json, pa.is_active
        FROM playbook_actions pa
        JOIN channel_types ct ON ct.id = pa.channel_id
        WHERE pa.id = :id AND pa.tenant_id = :tid
    """), {"id": target_action_id, "tid": db.info.get("tenant_id")})
    r = row.first()
    return PlaybookActionOut(
        id=r.id, action_order=r.action_order, channel_code=r.channel_code,
        trigger=r.trigger,
        trigger_config_json=_to_dict(r.trigger_config_json),
        ai_personalization_mode=r.ai_personalization_mode,
        subject_template=r.subject_template,
        body_template=r.body_template,
        task_template=r.task_template,
        day_offset=r.day_offset,
        skip_conditions_json=_to_dict(r.skip_conditions_json),
        is_active=r.is_active,
    )


@router.delete("/playbook-actions/{action_id}", status_code=204)
async def delete_playbook_action(
    action_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    """Soft-delete: mark step inactive."""
    result = await db.execute(text("""
        UPDATE playbook_actions SET is_active = FALSE, updated_at = NOW()
        WHERE id = :id AND tenant_id = :tid RETURNING id
    """), {"id": action_id, "tid": db.info.get("tenant_id")})
    if result.first() is None:
        raise HTTPException(status_code=404, detail="action not found")
    await db.commit()


@router.post("/playbook-actions/{action_id}/reorder",
             response_model=PlaybookActionOut)
async def reorder_playbook_action(
    action_id: int,
    body: ReorderRequest,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> PlaybookActionOut:
    """Move a step to a new position. Other steps shift to fill the gap."""
    # Find current position
    cur_row = await db.execute(text(
        "SELECT playbook_id, action_order FROM playbook_actions WHERE id = :id AND tenant_id = :tid"
    ), {"id": action_id, "tid": db.info.get("tenant_id")})
    cur = cur_row.first()
    if cur is None:
        raise HTTPException(status_code=404, detail="action not found")

    if cur.action_order == body.new_order_index:
        # No change needed; just return current
        row = await db.execute(text("""
            SELECT pa.id, pa.action_order, ct.code AS channel_code,
                   pa.trigger, pa.trigger_config_json, pa.ai_personalization_mode,
                   pa.subject_template, pa.body_template, pa.task_template,
                   pa.day_offset, pa.skip_conditions_json, pa.is_active
            FROM playbook_actions pa
            JOIN channel_types ct ON ct.id = pa.channel_id
            WHERE pa.id = :id AND pa.tenant_id = :tid
        """), {"id": action_id, "tid": db.info.get("tenant_id")})
        r = row.first()
        return PlaybookActionOut(
            id=r.id, action_order=r.action_order, channel_code=r.channel_code,
            trigger=r.trigger,
            trigger_config_json=_to_dict(r.trigger_config_json),
            ai_personalization_mode=r.ai_personalization_mode,
            subject_template=r.subject_template,
            body_template=r.body_template,
            task_template=r.task_template,
            day_offset=r.day_offset,
            skip_conditions_json=_to_dict(r.skip_conditions_json),
            is_active=r.is_active,
        )

    # Version-bump if in use
    target_pb_id = cur.playbook_id
    target_action_id = action_id
    if await _has_active_enrollments(db, cur.playbook_id):
        new_pb_id = await _create_new_version(
            db, old_playbook_id=cur.playbook_id, metadata_updates={},
            user_id=current_user.id,
        )
        target_pb_id = new_pb_id
        # Find cloned action by order
        cloned_row = await db.execute(text("""
            SELECT id FROM playbook_actions
            WHERE playbook_id = :pb AND tenant_id = :tid
              AND action_order = :ord AND is_active = TRUE
        """), {"pb": new_pb_id, "ord": cur.action_order, "tid": db.info.get("tenant_id")})
        target_action_id = cloned_row.first().id

    old_order = cur.action_order
    new_order = body.new_order_index

    # Shift other steps to fill the gap. Two-phase to avoid UNIQUE collision
    # on (playbook_id, action_order) WHERE is_active.
    # Phase 1: move target to a temporary high order
    temp_order = 9999
    await db.execute(text("""
        UPDATE playbook_actions SET action_order = :temp
        WHERE id = :id AND tenant_id = :tid
    """), {"temp": temp_order, "id": target_action_id, "tid": db.info.get("tenant_id")})

    if new_order > old_order:
        # Moving down — shift items in (old_order, new_order] up by one
        await db.execute(text("""
            UPDATE playbook_actions
            SET action_order = action_order - 1
            WHERE playbook_id = :pb
              AND tenant_id = :tid
              AND is_active = TRUE
              AND action_order > :old AND action_order <= :new
        """), {"pb": target_pb_id, "old": old_order, "new": new_order, "tid": db.info.get("tenant_id")})
    else:
        # Moving up — shift items in [new_order, old_order) down by one
        await db.execute(text("""
            UPDATE playbook_actions
            SET action_order = action_order + 1
            WHERE playbook_id = :pb
              AND tenant_id = :tid
              AND is_active = TRUE
              AND action_order >= :new AND action_order < :old
        """), {"pb": target_pb_id, "old": old_order, "new": new_order, "tid": db.info.get("tenant_id")})

    # Phase 2: place target at new_order
    await db.execute(text("""
        UPDATE playbook_actions SET action_order = :new WHERE id = :id AND tenant_id = :tid
    """), {"new": new_order, "id": target_action_id, "tid": db.info.get("tenant_id")})
    await db.commit()

    # Return updated action
    row = await db.execute(text("""
        SELECT pa.id, pa.action_order, ct.code AS channel_code,
               pa.trigger, pa.trigger_config_json, pa.ai_personalization_mode,
               pa.subject_template, pa.body_template, pa.task_template,
               pa.day_offset, pa.skip_conditions_json, pa.is_active
        FROM playbook_actions pa
        JOIN channel_types ct ON ct.id = pa.channel_id
        WHERE pa.id = :id AND pa.tenant_id = :tid
    """), {"id": target_action_id, "tid": db.info.get("tenant_id")})
    r = row.first()
    return PlaybookActionOut(
        id=r.id, action_order=r.action_order, channel_code=r.channel_code,
        trigger=r.trigger,
        trigger_config_json=_to_dict(r.trigger_config_json),
        ai_personalization_mode=r.ai_personalization_mode,
        subject_template=r.subject_template,
        body_template=r.body_template,
        task_template=r.task_template,
        day_offset=r.day_offset,
        skip_conditions_json=_to_dict(r.skip_conditions_json),
        is_active=r.is_active,
    )


# ════════════════════════════════════════════════════════════════════════════
# Test-send (dry-run rendering)
# ════════════════════════════════════════════════════════════════════════════

@router.post("/playbook-actions/{action_id}/test-send",
             response_model=TestSendResponse)
async def test_send_playbook_action(
    action_id: int,
    body: TestSendRequest,
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
) -> TestSendResponse:
    """Render templates against a real or synthetic contact. Does NOT
    dispatch — purely a preview."""
    row = await db.execute(text("""
        SELECT pa.id, ct.code AS channel_code,
               pa.subject_template, pa.body_template, pa.task_template
        FROM playbook_actions pa
        JOIN channel_types ct ON ct.id = pa.channel_id
        WHERE pa.id = :id AND pa.tenant_id = :tid
    """), {"id": action_id, "tid": db.info.get("tenant_id")})
    step = row.first()
    if step is None:
        raise HTTPException(status_code=404, detail="action not found")

    # Build render context
    ctx = {}
    if body.contact_id:
        c_row = await db.execute(text("""
            SELECT c.first_name, c.last_name, c.email, c.phone,
                   co.name AS company_name
            FROM contacts c
            JOIN companies co ON co.id = c.company_id
            WHERE c.id = :id AND c.tenant_id = :tid AND co.tenant_id = :tid
        """), {"id": body.contact_id, "tid": db.info.get("tenant_id")})
        c = c_row.first()
        if c is None:
            raise HTTPException(status_code=404, detail="contact not found")
        ctx = {
            "first_name": c.first_name or "",
            "last_name": c.last_name or "",
            "full_name": f"{c.first_name or ''} {c.last_name or ''}".strip(),
            "email": c.email or "",
            "phone": c.phone or "",
            "company_name": c.company_name or "",
        }
    if body.sample_contact:
        ctx.update(body.sample_contact)

    rendered_subj = _render_template(step.subject_template, ctx)
    rendered_body = _render_template(step.body_template, ctx)
    rendered_task = _render_template(step.task_template, ctx)

    warnings = []
    warnings.extend(_placeholder_warnings(rendered_subj))
    warnings.extend(_placeholder_warnings(rendered_body))
    warnings.extend(_placeholder_warnings(rendered_task))
    warnings = sorted(set(warnings))

    return TestSendResponse(
        channel_code=step.channel_code,
        rendered_subject=rendered_subj,
        rendered_body=rendered_body,
        rendered_task=rendered_task,
        placeholder_warnings=warnings,
    )
