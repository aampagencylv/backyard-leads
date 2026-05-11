"""Missive REST API client — server-side calls only.

Wraps the three endpoints we need for the sidebar v2:

  GET  /v1/users           → list team members (for sender heuristic)
  GET  /v1/shared_labels   → list org labels (for status → tag mapping)
  POST /v1/posts           → swiss-army endpoint that can:
                               - drop a comment in a conversation
                               - apply / remove shared labels
                               - close / reopen the conversation
                               - assign users
                             … all in one request.

Auth: `Authorization: Bearer <missive_api_token>`. The token comes from
settings.missive_api_token (env: MISSIVE_API_TOKEN).

Caching: users + labels are cached in-process for 5 minutes. Acceptable
staleness for org-level metadata that rarely changes. The cache is
per-worker; with one uvicorn worker on the VPS that's fine.
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger("bmp.missive_client")

API_BASE = "https://public.missiveapp.com/v1"
_CACHE_TTL_SECONDS = 300  # 5 minutes


# ============================================================
# In-process caches
# ============================================================

_users_cache: tuple[float, list[dict]] | None = None
_labels_cache: tuple[float, list[dict]] | None = None
_cache_lock = asyncio.Lock()


def _headers() -> dict:
    """Auth + JSON-content headers. Returns empty dict when no token
    configured so callers can short-circuit instead of 401-spamming."""
    tok = (settings.missive_api_token or "").strip()
    if not tok:
        return {}
    return {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
    }


def is_configured() -> bool:
    """True when a Missive token is plumbed in. Use this before
    showing any write-action UI in the sidebar."""
    return bool((settings.missive_api_token or "").strip())


# ============================================================
# Read endpoints (cached)
# ============================================================

async def list_users(*, refresh: bool = False) -> list[dict]:
    """Return all users in the Missive organization. Each item has
    at minimum {id, email, name, avatar_url}. Empty list on auth /
    network failure."""
    global _users_cache
    if not is_configured():
        return []
    async with _cache_lock:
        if not refresh and _users_cache and (time.monotonic() - _users_cache[0]) < _CACHE_TTL_SECONDS:
            return _users_cache[1]
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{API_BASE}/users", headers=_headers())
            if r.status_code != 200:
                log.warning(f"GET /v1/users → {r.status_code}: {r.text[:200]}")
                return _users_cache[1] if _users_cache else []
            users = (r.json() or {}).get("users") or []
            _users_cache = (time.monotonic(), users)
            return users
        except Exception as e:
            log.warning(f"GET /v1/users failed: {e}")
            return _users_cache[1] if _users_cache else []


async def team_emails(*, refresh: bool = False) -> set[str]:
    """Lowercased set of all team member email addresses. Used by the
    sidebar to skip BDR addresses when picking the prospect's email
    from a conversation."""
    users = await list_users(refresh=refresh)
    return {(u.get("email") or "").strip().lower() for u in users if u.get("email")}


async def list_shared_labels(*, refresh: bool = False) -> list[dict]:
    """Return all org-shared labels. Each item: {id, name, color,
    name_with_parent_names, ...}. Empty list on failure."""
    global _labels_cache
    if not is_configured():
        return []
    async with _cache_lock:
        if not refresh and _labels_cache and (time.monotonic() - _labels_cache[0]) < _CACHE_TTL_SECONDS:
            return _labels_cache[1]
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{API_BASE}/shared_labels", headers=_headers())
            if r.status_code != 200:
                log.warning(f"GET /v1/shared_labels → {r.status_code}: {r.text[:200]}")
                return _labels_cache[1] if _labels_cache else []
            labels = (r.json() or {}).get("shared_labels") or []
            _labels_cache = (time.monotonic(), labels)
            return labels
        except Exception as e:
            log.warning(f"GET /v1/shared_labels failed: {e}")
            return _labels_cache[1] if _labels_cache else []


async def find_label_id_by_name(name: str) -> Optional[str]:
    """Case-insensitive name match against the cached label list.
    Returns None when no label with that name exists — callers should
    treat 'no matching label' as 'skip this side effect', not 'error'."""
    target = (name or "").strip().lower()
    if not target:
        return None
    for lbl in await list_shared_labels():
        if (lbl.get("name") or "").strip().lower() == target:
            return lbl.get("id")
    return None


# ============================================================
# Write endpoint (POST /v1/posts)
# ============================================================

async def create_post(
    conversation_id: str,
    *,
    text: str,
    notification_title: str,
    notification_body: str,
    add_label_ids: Optional[list[str]] = None,
    remove_label_ids: Optional[list[str]] = None,
    username: str = "Prospector",
    username_icon: Optional[str] = None,
    close: bool = False,
    reopen: bool = False,
) -> dict:
    """POST /v1/posts — the swiss-army endpoint. Drops a comment in
    the conversation and optionally applies/removes labels in the
    same call.

    The `notification` block is mandatory per Missive's spec; we use
    it for the toast that teammates see when the post lands.

    Returns the parsed response dict on success, or {"_error": "..."}
    on failure. Never raises — callers should treat write failures as
    soft (Missive being down shouldn't block a CRM status change).
    """
    if not is_configured() or not conversation_id:
        return {"_error": "missive not configured or no conversation_id"}
    payload: dict = {
        "posts": {
            "conversation": conversation_id,
            "notification": {
                "title": notification_title[:80] or "Prospector",
                "body": notification_body[:200] or "",
            },
            "text": text[:8000],
            "username": username[:80],
        }
    }
    if username_icon:
        payload["posts"]["username_icon"] = username_icon
    if add_label_ids:
        payload["posts"]["add_shared_labels"] = list(add_label_ids)
    if remove_label_ids:
        payload["posts"]["remove_shared_labels"] = list(remove_label_ids)
    if close:
        payload["posts"]["close"] = True
    if reopen:
        payload["posts"]["reopen"] = True

    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.post(f"{API_BASE}/posts", headers=_headers(), json=payload)
        if r.status_code in (200, 201):
            return r.json() or {}
        log.warning(f"POST /v1/posts → {r.status_code}: {r.text[:300]}")
        return {"_error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        log.warning(f"POST /v1/posts failed: {e}")
        return {"_error": str(e)}


# ============================================================
# Convenience: prospector-status → missive-label mapping
# ============================================================

# Map our internal company.status values to the Missive label names we
# want applied to the conversation. The label names must already exist
# in the Missive org (we never auto-create labels — that's a manual
# governance step in Missive admin). Missing labels are skipped silently.
STATUS_TO_LABEL_NAME: dict[str, str] = {
    "qualified":      "Qualified Lead",
    "replied":        "Replied",
    "converted":      "Converted",
    "not_interested": "Not Interested",
    "sequencing":     "In Sequence",
    "contacted":      "Contacted",
}

# Inverse — when a status flips, we may also want to REMOVE the old
# labels so the conversation only carries the current state.
ALL_STATUS_LABEL_NAMES: list[str] = list(STATUS_TO_LABEL_NAME.values())


async def sync_status_label(
    *,
    conversation_id: str,
    new_status: str,
    contact_name: str = "",
    company_name: str = "",
    actor: str = "Prospector",
) -> dict:
    """High-level helper: drop a small comment + swap labels so the
    Missive conversation reflects the current prospector status.

    No-ops gracefully when:
      - missive not configured
      - no conversation_id stored for the contact
      - no matching label found in Missive (label name must exist)
    """
    if not is_configured() or not conversation_id:
        return {"_error": "missive not configured or no conversation_id"}

    add_id = await find_label_id_by_name(STATUS_TO_LABEL_NAME.get(new_status, ""))
    # Build the remove list from EVERY status label except the one we
    # just added — keeps the conversation cleanly tagged with one
    # status at a time without nuking unrelated labels.
    remove_ids: list[str] = []
    for s, label_name in STATUS_TO_LABEL_NAME.items():
        if s == new_status:
            continue
        lid = await find_label_id_by_name(label_name)
        if lid:
            remove_ids.append(lid)

    label_display = STATUS_TO_LABEL_NAME.get(new_status, new_status)
    text = (
        f"Status changed to **{label_display}**.\n\n"
        f"Contact: {contact_name or '(unknown)'}\n"
        f"Company: {company_name or '(unknown)'}"
    )
    return await create_post(
        conversation_id=conversation_id,
        text=text,
        notification_title=f"Prospector → {label_display}",
        notification_body=f"{contact_name or company_name}".strip(),
        add_label_ids=[add_id] if add_id else None,
        remove_label_ids=remove_ids or None,
        username=actor,
    )
