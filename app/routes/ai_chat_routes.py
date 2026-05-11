"""
In-app AI chatbot — POST /api/ai/chat.

The chatbot widget in the corner of the app sends conversation history
here and gets back an assistant reply. Under the hood we run Anthropic's
tool-use loop against the exact same TOOL_HANDLERS the MCP server
exposes — so anything Claude can do through MCP, the in-app chatbot
can do too, with no duplicate tool code.

Auth: standard get_current_user (JWT). The user is fully authenticated;
we skip the API-key scope gate that MCP uses since this is an in-app
session, not an external integration. Multi-tenant scoping still
applies — sales_reps only see their own companies because each tool
handler calls scope_companies / scope_contacts internally.

v1 scope:
  - Non-streaming (request → final reply in one shot)
  - 10-turn tool-use cap to prevent runaway
  - Conversation history client-side (localStorage); the client posts
    the full history each turn. Capped at 50 messages server-side to
    bound token cost.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import User
from app.services.mcp_tools import TOOL_DEFINITIONS, TOOL_HANDLERS

log = logging.getLogger("bmp.ai_chat")

router = APIRouter(prefix="/api/ai", tags=["ai-chat"])


MAX_TURNS = 10              # Max iterations of the tool-use loop
MAX_HISTORY_MESSAGES = 50   # Trim client-supplied history to last N
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048


# Anthropic's tool format uses input_schema (snake_case); MCP uses
# inputSchema (camelCase). Adapter strips down to the fields Anthropic
# expects and translates the key name.
def _to_anthropic_tools(mcp_tools: list[dict]) -> list[dict]:
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["inputSchema"],
        }
        for t in mcp_tools
    ]


def _system_prompt(user: User) -> str:
    """Compact system prompt — Claude has a small attention budget,
    the bigger the prompt the less room for tool descriptions + the
    conversation. Be specific about behavior, terse on background."""
    today = datetime.now(timezone.utc).strftime("%A, %B %-d, %Y")
    name = f"{user.first_name} {user.last_name}".strip() or user.email
    return (
        "You are an AI assistant inside the Backyard Marketing Pros CRM "
        "(Prospector). You help sales reps research prospects, surface "
        "hot leads, schedule meetings, add notes, and answer questions "
        "about their pipeline.\n\n"
        f"Current user: {name} (role: {user.role}). Today is {today}.\n\n"
        "RULES:\n"
        "1. Always call tools to get current data. Don't make up information "
        "about companies, contacts, or pipeline state — use the tools.\n"
        "2. For mutating tools (add_note, create_task, update_*, "
        "start_sequence, book_meeting, tag_company, etc.), briefly state "
        "what you're about to do BEFORE calling the tool. The UI shows "
        "the user every action.\n"
        "3. Format results compactly. Companies as 'Smith Pools "
        "(score 67, Phoenix AZ)'. Contacts as 'John Smith — CEO at Acme'. "
        "Use bullet lists for multi-item results.\n"
        "4. If a request is ambiguous (e.g. 'add a note' without saying "
        "which company), ask a clarifying question — don't guess.\n"
        "5. When the user asks about 'me' / 'my pipeline' / etc., it "
        "refers to the current user above.\n"
        "6. Keep responses tight. Sales reps are pressed for time; "
        "1-3 sentences + maybe a list is usually right."
    )


# ============================================================
# Request / response shapes
# ============================================================

class ChatMessage(BaseModel):
    role: str  # 'user' or 'assistant'
    content: str  # plain text; tool exchanges are server-internal


class ChatRequest(BaseModel):
    messages: list[ChatMessage]


class ChatResponse(BaseModel):
    reply: str
    tool_calls: list[dict] = []  # surface to the UI so user sees what was done
    turns: int = 0


# ============================================================
# Tool execution wrapper
# ============================================================

async def _execute_tool(
    name: str, arguments: dict, db: AsyncSession, user: User,
) -> dict:
    """Run a tool and return its JSON-serializable result. Errors
    become structured error dicts that the model can read and adapt
    its next move to — never raise out of this function."""
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return {"error": "unknown_tool", "tool": name}
    try:
        result = await handler(db, user, **(arguments or {}))
        return result if isinstance(result, dict) else {"data": result}
    except TypeError as e:
        # Bad argument shape — let the model self-correct
        return {"error": "invalid_arguments", "tool": name, "detail": str(e)[:200]}
    except Exception as e:
        log.exception(f"AI chat tool '{name}' failed: {e}")
        return {"error": "tool_execution_failed", "tool": name,
                "detail": f"{type(e).__name__}: {str(e)[:200]}"}


# ============================================================
# Endpoint
# ============================================================

@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run one conversation turn. The client sends the full history;
    we run the tool-use loop and return the final assistant reply
    plus a summary of any tools called (so the UI can surface a
    'used X tools' indicator next to the message)."""
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="AI assistant not configured (ANTHROPIC_API_KEY missing on server)",
        )

    # Trim client-supplied history (defense against runaway tokens).
    # Last MAX_HISTORY_MESSAGES, dropping anything that isn't user/assistant.
    raw = body.messages or []
    raw = [m for m in raw if m.role in ("user", "assistant") and m.content]
    raw = raw[-MAX_HISTORY_MESSAGES:]
    if not raw or raw[-1].role != "user":
        raise HTTPException(status_code=400, detail="Last message must be from the user")

    # Anthropic message format. Tool exchanges accumulate inside this
    # list as we loop; the client never sees them — we only return the
    # final assistant text and a summary of tool calls made.
    messages: list[dict] = [
        {"role": m.role, "content": m.content} for m in raw
    ]

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    tools = _to_anthropic_tools(TOOL_DEFINITIONS)
    system = _system_prompt(user)

    tool_calls_summary: list[dict] = []
    turns = 0

    while turns < MAX_TURNS:
        turns += 1
        try:
            resp = await client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=tools,
                messages=messages,
            )
        except anthropic.APIError as e:
            log.exception(f"Anthropic API error: {e}")
            raise HTTPException(status_code=502, detail=f"AI service error: {e}")

        # If Claude wants to call a tool, execute it + loop. Otherwise
        # we have our final answer.
        if resp.stop_reason != "tool_use":
            # Collect text blocks from final response
            text_parts: list[str] = []
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    text_parts.append(block.text)
            reply = "\n".join(text_parts).strip() or "(no response)"
            # Meter the AI call (best-effort, never blocks response)
            try:
                from app.services.credit_meter import meter, make_idem_key
                await meter(
                    db, action_type="ai_chat_turn",
                    idempotency_key=make_idem_key("ai_chat", user.id,
                                                  datetime.now(timezone.utc).timestamp()),
                    user_id=user.id, action_ref=f"chat:{user.id}",
                    metadata={"turns": turns, "tool_calls": len(tool_calls_summary)},
                )
            except Exception:
                pass
            return ChatResponse(reply=reply, tool_calls=tool_calls_summary, turns=turns)

        # tool_use path: append the assistant's tool-call message, then
        # append a user message with tool_result blocks containing what
        # we got back. Loop continues with this richer context.
        messages.append({"role": "assistant", "content": [
            block.model_dump() for block in resp.content
        ]})
        tool_results = []
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                name = block.name
                args = block.input or {}
                result = await _execute_tool(name, args, db, user)
                tool_calls_summary.append({"name": name, "input": args,
                                            "ok": "error" not in result})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })
        messages.append({"role": "user", "content": tool_results})
        # Continue the loop — Claude now has the tool results

    # Loop hit MAX_TURNS without finishing. Should be rare; surface
    # as a friendly truncation instead of a 500.
    return ChatResponse(
        reply="I ran out of steps trying to answer that. Could you break it into smaller questions?",
        tool_calls=tool_calls_summary, turns=turns,
    )
