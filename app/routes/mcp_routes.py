"""
MCP server endpoint.

Implements just enough of the Model Context Protocol (2025-06-18 spec)
to be a useful remote MCP server: capability negotiation, tools/list,
tools/call, resources/list, resources/read. JSON-RPC 2.0 over HTTP —
no SSE / streaming required for our read-only v1.

Auth: standard X-API-Key header against the existing api_keys table.
The same API key the user already uses for the public REST API works
here — no second credential to manage.

Connect from Claude Desktop / Claude.ai / ChatGPT MCP:
  URL:  https://prospector.backyardmarketingpros.com/mcp
  Auth header: X-API-Key: <their-api-key>
"""
from __future__ import annotations
import json
import logging
import urllib.parse
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_user_from_api_key
from app.database import get_db
from app.models import User
from app.services.mcp_tools import (
    TOOL_DEFINITIONS, TOOL_HANDLERS, WRITE_TOOL_NAMES,
    get_company, get_contact,
)

log = logging.getLogger("bmp.mcp")

# Single endpoint: POST /mcp. Standard JSON-RPC 2.0 envelope in/out.
# We don't use a path prefix because MCP clients expect a root URL.
router = APIRouter(tags=["mcp"])

# Spec version we conform to. Bump if we adopt newer MCP semantics.
MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "backyard-leads-prospector"
SERVER_VERSION = "0.1.0"


# ============================================================
# JSON-RPC 2.0 helpers
# ============================================================

def _ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str, data: Optional[Any] = None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# ============================================================
# Method handlers
# ============================================================

async def _handle_initialize(params: dict) -> dict:
    """Capability negotiation. Echo back the client's protocol version
    when sane; otherwise serve our default."""
    client_version = (params or {}).get("protocolVersion") or MCP_PROTOCOL_VERSION
    return {
        "protocolVersion": client_version,
        "serverInfo": {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
        },
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"listChanged": False, "subscribe": False},
            "logging": {},
        },
        "instructions": (
            "Backyard Leads (BMP Prospector) CRM — search prospects, pull "
            "company / contact records, surface hot leads + recent replies, "
            "generate AI briefs. All queries are scoped to the API key's "
            "owner; sales reps see only their own data, admins see all."
        ),
    }


async def _handle_tools_list() -> dict:
    return {"tools": TOOL_DEFINITIONS}


async def _handle_tools_call(params: dict, db: AsyncSession, user: User) -> dict:
    name = (params or {}).get("name")
    arguments = (params or {}).get("arguments") or {}
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
        }
    # Scope gate: write tools require an API key with scope='write'.
    # The auth helper stamps _api_key_scope on the user object.
    if name in WRITE_TOOL_NAMES:
        scope = getattr(user, "_api_key_scope", "read") or "read"
        if scope != "write":
            return {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": (
                        f"Permission denied: '{name}' requires an API key with "
                        f"scope='write'. The current key has scope='{scope}'. "
                        "Generate a new key in Settings → 🔑 API Keys & Webhooks "
                        "with the Write scope."
                    ),
                }],
            }
    try:
        result = await handler(db, user, **arguments)
    except TypeError as e:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Invalid arguments for {name}: {e}"}],
        }
    except Exception as e:
        log.exception(f"Tool {name} failed: {e}")
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Tool execution error: {e}"}],
        }
    # MCP tools return a content array. We pack the JSON result as a
    # text block — clients (Claude, GPT) will parse it. Some clients
    # also accept structured 'json' content type but text is universal.
    return {
        "content": [{"type": "text", "text": json.dumps(result, default=str)}],
        "isError": False,
    }


# ----- Resources ----------------------------------------------------

# Resources are read-only references the AI can fetch by URI. We
# expose three URI patterns — a tool can return a structured ID and
# the client can later call resources/read to get the full record.
# Right now resources/read just routes back into the same get_*
# tool handlers since the response shape is the same.

RESOURCES_TEMPLATE = [
    {
        "uriTemplate": "prospector://companies/{id}",
        "name": "Company",
        "description": "Full CRM record for a company by id.",
        "mimeType": "application/json",
    },
    {
        "uriTemplate": "prospector://contacts/{id}",
        "name": "Contact",
        "description": "Full contact record + recent activity by id.",
        "mimeType": "application/json",
    },
]


async def _handle_resources_list() -> dict:
    """We don't enumerate the full row set — the user's CRM has thousands
    of rows. Instead we return resourceTemplates so clients understand
    the URI shape and can construct fetches from tool responses."""
    return {"resources": [], "resourceTemplates": RESOURCES_TEMPLATE}


async def _handle_resources_read(params: dict, db: AsyncSession, user: User) -> dict:
    uri = (params or {}).get("uri")
    if not uri:
        raise ValueError("Missing 'uri'")
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "prospector":
        raise ValueError(f"Unsupported scheme: {parsed.scheme}")
    # path is like '/companies/123'  → strip leading slash, split
    parts = (parsed.netloc + parsed.path).strip("/").split("/")
    # Some URI implementations land the host before /, some after — handle both
    parts = [p for p in parts if p]
    if len(parts) != 2:
        raise ValueError(f"Bad URI shape: {uri}")
    kind, raw_id = parts
    try:
        rid = int(raw_id)
    except ValueError:
        raise ValueError(f"Bad id in URI: {raw_id}")
    if kind == "companies":
        result = await get_company(db, user, company_id=rid)
    elif kind == "contacts":
        result = await get_contact(db, user, contact_id=rid)
    else:
        raise ValueError(f"Unsupported resource kind: {kind}")
    return {
        "contents": [{
            "uri": uri,
            "mimeType": "application/json",
            "text": json.dumps(result, default=str),
        }],
    }


# ============================================================
# HTTP entrypoint — POST /mcp
# ============================================================

@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_user_from_api_key),
):
    """Single JSON-RPC 2.0 endpoint. Accepts one request OR a batch
    (array of requests, processed sequentially — we don't parallelize
    since DB access is per-session)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_err(None, -32700, "Parse error: not valid JSON"), status_code=400)

    if isinstance(body, list):
        # Batch request — return an array of responses
        responses = []
        for item in body:
            if not isinstance(item, dict):
                responses.append(_err(None, -32600, "Invalid Request"))
                continue
            r = await _dispatch(item, db, user)
            if r is not None:  # notifications return None (no id)
                responses.append(r)
        if not responses:
            return Response(status_code=204)
        return JSONResponse(responses)

    if not isinstance(body, dict):
        return JSONResponse(_err(None, -32600, "Invalid Request: must be object or array"), status_code=400)

    response = await _dispatch(body, db, user)
    if response is None:
        # Notification — no response body per JSON-RPC 2.0
        return Response(status_code=204)
    return JSONResponse(response)


async def _dispatch(item: dict, db: AsyncSession, user: User) -> Optional[dict]:
    """Route a single JSON-RPC request to the appropriate MCP method
    handler. Returns None for notifications (requests with no id)."""
    req_id = item.get("id")
    method = item.get("method") or ""
    params = item.get("params") or {}
    is_notification = "id" not in item

    try:
        if method == "initialize":
            result = await _handle_initialize(params)
        elif method == "ping":
            # Standard JSON-RPC ping; clients use this to check the
            # connection is alive. Returns {} per MCP spec.
            result = {}
        elif method == "notifications/initialized":
            # Client signalling it finished initialization. No response.
            return None
        elif method == "tools/list":
            result = await _handle_tools_list()
        elif method == "tools/call":
            result = await _handle_tools_call(params, db, user)
        elif method == "resources/list":
            result = await _handle_resources_list()
        elif method == "resources/read":
            result = await _handle_resources_read(params, db, user)
        else:
            if is_notification:
                return None
            return _err(req_id, -32601, f"Method not found: {method}")
    except ValueError as e:
        if is_notification:
            return None
        return _err(req_id, -32602, f"Invalid params: {e}")
    except Exception as e:
        log.exception(f"MCP dispatch error on method {method}: {e}")
        if is_notification:
            return None
        return _err(req_id, -32603, f"Internal error: {e}")

    if is_notification:
        return None
    return _ok(req_id, result)


# ============================================================
# Discovery — GET /mcp returns server info as JSON
# ============================================================

@router.get("/mcp")
async def mcp_info():
    """Friendly GET handler so curl-ing the URL shows a 'this is an
    MCP endpoint' page instead of a 405. Useful for ops + setup
    troubleshooting. No auth required for this — it's just metadata."""
    return JSONResponse({
        "kind": "mcp-server",
        "name": SERVER_NAME,
        "version": SERVER_VERSION,
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "transport": "http",
        "method": "POST",
        "auth": {
            "scheme": "ApiKey",
            "header": "X-API-Key",
            "where": "Generate one in Settings → 🔑 API Keys & Webhooks",
        },
        "tool_count": len(TOOL_DEFINITIONS),
        "tools": [t["name"] for t in TOOL_DEFINITIONS],
        "docs": "POST a JSON-RPC 2.0 request to this URL. See https://modelcontextprotocol.io for the protocol spec.",
    })
