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
    decision = get_orchestrator().decide(payload.message, context=payload.context)

    data = None
    if decision.tool:
        tool = registry.get(decision.tool)
        if tool is not None:
            params = dict(decision.params)
            if payload.zone_id is not None:
                params.setdefault("zone_id", payload.zone_id)
            data = tool.handler(request.auth, params)
    elif decision.intent == "commands":
        # No DB needed — hand back the catalog so the UI can render it.
        data = {"commands": registry.catalog()}

    return {
        "intent": decision.intent,
        "reply_key": decision.reply_key,
        "tool": decision.tool,
        "data": data,
    }
