"""Helpers around the engagement-phase state machine.

The DB enforces transitions via the `enforce_phase_transition()` trigger
against the `phase_transitions` lookup table. This module helps the
AI decision layer ONLY propose transitions that the DB will accept — so
the LLM doesn't waste tokens producing illegal choices.

Workflow:
  1. Decision maker fetches the legal transitions for the engagement's
     current_phase and the proposing actor ('ai').
  2. Builds the prompt with ONLY those legal transitions as choices.
  3. The LLM picks one (or 'no_transition').
  4. DB trigger validates again as a final defense; should always agree.
"""
from __future__ import annotations
from typing import Iterable
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def legal_transitions_from(
    session: AsyncSession, *, from_phase: str, by: str,
) -> list[dict]:
    """Return the list of legal `to_phase` values reachable from
    `from_phase` by the given actor. Each row includes the optional
    `requires_status` precondition (None = no precondition).
    """
    rows = await session.execute(text("""
        SELECT to_phase, requires_status
        FROM phase_transitions
        WHERE from_phase = :f AND allowed_by = :by
        ORDER BY to_phase
    """), {"f": from_phase, "by": by})
    return [{"to_phase": r.to_phase, "requires_status": r.requires_status}
            for r in rows]


def format_transitions_for_prompt(
    transitions: Iterable[dict], current_status: str,
) -> str:
    """Format the legal transitions as a JSON-ish list for embedding in
    the LLM prompt. Filters to transitions whose `requires_status`
    matches the engagement's current status (or is None)."""
    legal = []
    for t in transitions:
        rs = t.get("requires_status")
        if rs is None or rs == current_status:
            legal.append(t["to_phase"])
    if not legal:
        return '[]  (no legal transitions from current phase)'
    return "[\"" + "\", \"".join(legal) + "\"]"
