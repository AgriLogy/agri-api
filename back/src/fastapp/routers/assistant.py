"""fastapp /assistant — tool catalog + per-tool invoke + orchestrated /chat +
server-side conversation history.

Strangler port of ``apps/assistant/router.py`` (django-ninja). Byte-parity with
the Django version on the deterministic surface:

* ``GET  /assistant/tools``               — the tool catalog.
* ``POST /assistant/tools/{name}``        — invoke one tool (404 ``{"detail":…}``
                                            for an unknown name).
* ``POST /assistant/chat``                — orchestrate a message → intent +
                                            reply_key/reply + tool + data.
* ``GET/PUT/DELETE /assistant/conversations[/{client_id}]`` — per-user history.

The tool catalog + conversation CRUD are deterministic (byte-identical). The
``/chat`` LLM reply is non-deterministic (model output); with no ``AI_API_KEY``
set — the parity default — the rule-based orchestrator drives ``/chat``
deterministically, so its envelope is byte-identical too.

Data access is SQLAlchemy via agri-core (no Django ORM). Conversation writes go
through ``session_scope(commit=True)`` against ``assistant_conversation``.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from agri.core.database import session_scope
from agri.db.assistant import AssistantConversation
from fastapp.auth import AuthedUser, get_current_user

from fastapp.assistant.orchestrator import get_orchestrator
from fastapp.assistant.tools import registry

router = APIRouter(tags=["assistant"])


class ToolInvokeIn(BaseModel):
    params: dict[str, Any] = {}


class ChatIn(BaseModel):
    message: str
    zone_id: int | None = None
    context: str | None = None


class ConversationIn(BaseModel):
    title: str = ""
    messages: list[dict] = []
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


def _serialize_conversation(c: AssistantConversation) -> dict:
    return {
        "id": c.client_id,
        "title": c.title,
        "messages": c.messages,
        "createdAt": c.created_at.isoformat(),
        "updatedAt": c.updated_at.isoformat(),
    }


@router.get("/assistant/tools", summary="List the assistant's available tools")
def list_tools(user: AuthedUser = Depends(get_current_user)):
    return {"tools": registry.catalog()}


@router.post("/assistant/tools/{name}", summary="Invoke a single assistant tool")
def invoke_tool(
    name: str,
    payload: ToolInvokeIn,
    user: AuthedUser = Depends(get_current_user),
):
    tool = registry.get(name)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")
    data = tool.handler(user, payload.params or {})
    return {"tool": name, "data": data}


@router.post(
    "/assistant/chat", summary="Understand a message and return the right data"
)
def chat(payload: ChatIn, user: AuthedUser = Depends(get_current_user)):
    def run_tool(name: str, params: dict) -> dict:
        tool = registry.get(name)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")
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


# ── conversation history (server-side, per user) ─────────────────────────────
@router.get("/assistant/conversations", summary="List the caller's conversations")
def list_conversations(user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        rows = session.scalars(
            select(AssistantConversation)
            .where(AssistantConversation.user_id == user.id)
            .order_by(AssistantConversation.updated_at.desc())
        ).all()
        return [_serialize_conversation(c) for c in rows]


@router.put(
    "/assistant/conversations/{client_id}",
    summary="Create or replace a conversation",
)
def upsert_conversation(
    client_id: str,
    payload: ConversationIn,
    user: AuthedUser = Depends(get_current_user),
):
    now = datetime.datetime.now(datetime.timezone.utc)
    with session_scope(commit=True) as session:
        obj = session.scalars(
            select(AssistantConversation).where(
                AssistantConversation.user_id == user.id,
                AssistantConversation.client_id == client_id,
            )
        ).first()
        title = (payload.title or "")[:200]
        messages = payload.messages or []
        created_at = payload.created_at or now
        updated_at = payload.updated_at or now
        if obj is None:
            obj = AssistantConversation(
                user_id=user.id,
                client_id=client_id,
                title=title,
                messages=messages,
                created_at=created_at,
                updated_at=updated_at,
            )
            session.add(obj)
        else:
            obj.title = title
            obj.messages = messages
            obj.created_at = created_at
            obj.updated_at = updated_at
        session.flush()
        return _serialize_conversation(obj)


@router.delete("/assistant/conversations/{client_id}", summary="Delete a conversation")
def delete_conversation(
    client_id: str,
    user: AuthedUser = Depends(get_current_user),
):
    with session_scope(commit=True) as session:
        obj = session.scalars(
            select(AssistantConversation).where(
                AssistantConversation.user_id == user.id,
                AssistantConversation.client_id == client_id,
            )
        ).first()
        if obj is not None:
            session.delete(obj)
    return {"deleted": True}
