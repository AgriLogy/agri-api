"""Assistant HTTP surface (mounted at /assistant).

Three endpoints, all auth-scoped to the caller:

  GET  /assistant/tools          — the tool catalog (what the assistant can do)
  POST /assistant/tools/{name}   — invoke a single tool with params (the "tool"
                                   the assistant calls over HTTP for its data)
  POST /assistant/chat           — orchestrate a message: understand it, pick a
                                   tool, run it, return the data + a reply key

The data layer (registry/tools) is the only thing that touches the DB; this
module just validates, authenticates, and serializes.
"""

from __future__ import annotations

from typing import Any

from ninja import Router, Schema
from ninja.errors import HttpError

from agriapi.api.auth import JwtAuth
from .orchestrator import get_orchestrator
from .tools import registry

router = Router()


class ToolInvokeIn(Schema):
    params: dict[str, Any] = {}


class ChatIn(Schema):
    message: str
    zone_id: int | None = None
    context: str | None = None


@router.get("/tools", auth=JwtAuth(), summary="List the assistant's available tools")
def list_tools(request):
    return {"tools": registry.catalog()}


@router.post("/tools/{name}", auth=JwtAuth(), summary="Invoke a single assistant tool")
def invoke_tool(request, name: str, payload: ToolInvokeIn):
    tool = registry.get(name)
    if tool is None:
        raise HttpError(404, f"Unknown tool: {name}")
    data = tool.handler(request.auth, payload.params or {})
    return {"tool": name, "data": data}


@router.post(
    "/chat", auth=JwtAuth(), summary="Understand a message and return the right data"
)
def chat(request, payload: ChatIn):
    user = request.auth

    def run_tool(name: str, params: dict) -> dict:
        tool = registry.get(name)
        if tool is None:
            raise HttpError(404, f"Unknown tool: {name}")
        merged = dict(params or {})
        if payload.zone_id is not None:
            merged.setdefault("zone_id", payload.zone_id)
        return tool.handler(user, merged)

    result = get_orchestrator().respond(
        payload.message, run_tool=run_tool, context=payload.context
    )

    return {
        "intent": result.intent,
        "reply_key": result.reply_key,
        "reply": result.reply,
        "tool": result.tool,
        "data": result.data,
    }
