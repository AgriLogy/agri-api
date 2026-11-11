"""fastapp /admin/assistant/knowledge — the assistant's knowledge base (#461).

Admin tier only. Staff write agronomy articles, send them for review, approve
and archive them. Only ``approved`` articles reach the assistant
(``search_knowledge`` tool), so nothing a farmer is told as general guidance
skipped a human review.

* ``GET    /admin/assistant/knowledge``              — list (+ filters, counts)
* ``POST   /admin/assistant/knowledge``              — create (draft or review)
* ``GET    /admin/assistant/knowledge/{id}``         — one article
* ``PUT    /admin/assistant/knowledge/{id}``         — edit; editing the content
                                                      of an approved article
                                                      sends it back to review
* ``POST   /admin/assistant/knowledge/{id}/status``  — draft / review /
                                                      approved / archived
* ``DELETE /admin/assistant/knowledge/{id}``         — delete

Approving needs at least one source and stamps the reviewer; an approval
without a future review date gets one a year out.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from agri.core.database import session_scope
from agri.db.assistant import (
    KNOWLEDGE_LANGUAGES,
    KNOWLEDGE_STATUSES,
    KNOWLEDGE_TOPICS,
    AssistantKnowledgeArticle,
)
from fastapp.adminutil import record_audit
from fastapp.assistant.knowledge import KNOWLEDGE_UNAVAILABLE, knowledge_available
from fastapp.auth import LEVEL_ADMIN, AuthedUser, get_current_user, level_rank
from fastapp.json import DjangoStyleJSONResponse

router = APIRouter(tags=["admin-assistant"])

BASE = "/admin/assistant/knowledge"

#: How long an approval holds when the reviewer sets no review date.
DEFAULT_REVIEW_PERIOD = datetime.timedelta(days=365)

_CONTENT_FIELDS = ("title", "language", "topic", "crops", "keywords", "body", "sources")


class SourceIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    url: str = Field(default="", max_length=500)


class ArticleIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    language: str
    topic: str
    body: str = Field(min_length=1, max_length=20000)
    keywords: str = Field(default="", max_length=2000)
    crops: list[str] = Field(default_factory=list, max_length=20)
    sources: list[SourceIn] = Field(default_factory=list, max_length=10)
    review_due: datetime.date | None = None
    # Creation only: start as a draft, or submit for review straight away.
    status: str = "draft"


class StatusIn(BaseModel):
    status: str
    review_due: datetime.date | None = None


def _require_admin(user: AuthedUser) -> None:
    if level_rank(user.access_level) < level_rank(LEVEL_ADMIN):
        raise HTTPException(status_code=403, detail="Admin access required")


def _bad(detail: str, status_code: int = 400):
    return DjangoStyleJSONResponse({"detail": detail}, status_code=status_code)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _out(row: AssistantKnowledgeArticle, today: datetime.date) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "language": row.language,
        "topic": row.topic,
        "crops": list(row.crops or []),
        "keywords": row.keywords,
        "body": row.body,
        "sources": list(row.sources or []),
        "status": row.status,
        "review_due": _iso(row.review_due),
        "review_overdue": bool(
            row.status == "approved" and row.review_due and row.review_due < today
        ),
        "reviewed_at": _iso(row.reviewed_at),
        "reviewed_by_id": row.reviewed_by_id,
        "created_by_id": row.created_by_id,
        "updated_by_id": row.updated_by_id,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _validate(payload: ArticleIn) -> str | None:
    if payload.language not in KNOWLEDGE_LANGUAGES:
        return f"language must be one of {list(KNOWLEDGE_LANGUAGES)}."
    if payload.topic not in KNOWLEDGE_TOPICS:
        return f"topic must be one of {list(KNOWLEDGE_TOPICS)}."
    if not payload.title.strip() or not payload.body.strip():
        return "title and body are required."
    for source in payload.sources:
        url = source.url.strip()
        if url and not url.startswith(("http://", "https://")):
            return "a source url must start with http:// or https://."
    if any(len(c.strip()) > 80 for c in payload.crops):
        return "a crop name is at most 80 characters."
    return None


def _content(payload: ArticleIn) -> dict[str, Any]:
    return {
        "title": payload.title.strip(),
        "language": payload.language,
        "topic": payload.topic,
        "crops": [c.strip() for c in payload.crops if c.strip()],
        "keywords": payload.keywords.strip(),
        "body": payload.body.strip(),
        "sources": [
            {"title": s.title.strip(), "url": s.url.strip()} for s in payload.sources
        ],
    }


def _approve(
    row: AssistantKnowledgeArticle,
    user: AuthedUser,
    now: datetime.datetime,
    review_due: datetime.date | None,
) -> str | None:
    if not row.sources:
        return "an article needs at least one source to be approved."
    if review_due is not None and review_due <= now.date():
        return "review_due must be in the future."
    row.status = "approved"
    row.reviewed_at = now
    row.reviewed_by_id = user.id
    if review_due is not None:
        row.review_due = review_due
    elif row.review_due is None or row.review_due <= now.date():
        row.review_due = now.date() + DEFAULT_REVIEW_PERIOD
    return None


def _get(session, article_id: int) -> AssistantKnowledgeArticle | None:
    return session.get(AssistantKnowledgeArticle, article_id)


@router.get(BASE, summary="Admin: list knowledge-base articles")
def list_articles(
    status: str | None = Query(default=None),
    topic: str | None = Query(default=None),
    language: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user: AuthedUser = Depends(get_current_user),
):
    _require_admin(user)
    meta = {
        "statuses": list(KNOWLEDGE_STATUSES),
        "topics": list(KNOWLEDGE_TOPICS),
        "languages": list(KNOWLEDGE_LANGUAGES),
    }
    with session_scope() as session:
        if not knowledge_available(session):
            return {
                "available": False,
                "detail": KNOWLEDGE_UNAVAILABLE,
                "items": [],
                "counts": {},
                **meta,
            }
        A = AssistantKnowledgeArticle
        stmt = select(A).order_by(A.updated_at.desc(), A.id.desc())
        if status in KNOWLEDGE_STATUSES:
            stmt = stmt.where(A.status == status)
        if topic in KNOWLEDGE_TOPICS:
            stmt = stmt.where(A.topic == topic)
        if language in KNOWLEDGE_LANGUAGES:
            stmt = stmt.where(A.language == language)
        if q and q.strip():
            like = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(A.title.ilike(like), A.keywords.ilike(like), A.body.ilike(like))
            )
        today = datetime.date.today()
        items = [_out(r, today) for r in session.execute(stmt).scalars()]
        counts = {s: 0 for s in KNOWLEDGE_STATUSES}
        for s, n in session.execute(select(A.status, func.count()).group_by(A.status)):
            counts[s] = n
        return {"available": True, "items": items, "counts": counts, **meta}


@router.post(BASE, summary="Admin: create a knowledge-base article", status_code=201)
def create_article(payload: ArticleIn, user: AuthedUser = Depends(get_current_user)):
    _require_admin(user)
    if payload.status not in ("draft", "review"):
        return _bad("a new article starts as draft or review; approve it after.")
    error = _validate(payload)
    if error:
        return _bad(error)
    now = datetime.datetime.now(datetime.timezone.utc)
    with session_scope(commit=True) as session:
        if not knowledge_available(session):
            return _bad(KNOWLEDGE_UNAVAILABLE)
        row = AssistantKnowledgeArticle(
            **_content(payload),
            status=payload.status,
            review_due=payload.review_due,
            created_by_id=user.id,
            updated_by_id=user.id,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        record_audit(
            session,
            user.id,
            "knowledge.create",
            "knowledge_article",
            row.id,
            {"title": row.title, "status": row.status},
        )
        return DjangoStyleJSONResponse(_out(row, now.date()), status_code=201)


@router.get(f"{BASE}/{{article_id}}", summary="Admin: one knowledge-base article")
def get_article(article_id: int, user: AuthedUser = Depends(get_current_user)):
    _require_admin(user)
    with session_scope() as session:
        if not knowledge_available(session):
            return _bad(KNOWLEDGE_UNAVAILABLE)
        row = _get(session, article_id)
        if row is None:
            return _bad("Article not found.", 404)
        return _out(row, datetime.date.today())


@router.put(f"{BASE}/{{article_id}}", summary="Admin: edit a knowledge-base article")
def update_article(
    article_id: int, payload: ArticleIn, user: AuthedUser = Depends(get_current_user)
):
    _require_admin(user)
    error = _validate(payload)
    if error:
        return _bad(error)
    now = datetime.datetime.now(datetime.timezone.utc)
    with session_scope(commit=True) as session:
        if not knowledge_available(session):
            return _bad(KNOWLEDGE_UNAVAILABLE)
        row = _get(session, article_id)
        if row is None:
            return _bad("Article not found.", 404)
        content = _content(payload)
        changed = sorted(f for f in _CONTENT_FIELDS if getattr(row, f) != content[f])
        for field, value in content.items():
            setattr(row, field, value)
        if payload.review_due != row.review_due:
            row.review_due = payload.review_due
            changed.append("review_due")
        # Reviewed text must stay reviewed text: a content edit to an approved
        # article takes it off the assistant until it is approved again.
        requeued = row.status == "approved" and any(
            f in _CONTENT_FIELDS for f in changed
        )
        if requeued:
            row.status = "review"
        row.updated_by_id = user.id
        row.updated_at = now
        record_audit(
            session,
            user.id,
            "knowledge.update",
            "knowledge_article",
            row.id,
            {"fields": changed, "back_to_review": requeued},
        )
        return _out(row, now.date())


@router.post(
    f"{BASE}/{{article_id}}/status",
    summary="Admin: move a knowledge-base article through review",
)
def set_status(
    article_id: int, payload: StatusIn, user: AuthedUser = Depends(get_current_user)
):
    _require_admin(user)
    if payload.status not in KNOWLEDGE_STATUSES:
        return _bad(f"status must be one of {list(KNOWLEDGE_STATUSES)}.")
    now = datetime.datetime.now(datetime.timezone.utc)
    with session_scope(commit=True) as session:
        if not knowledge_available(session):
            return _bad(KNOWLEDGE_UNAVAILABLE)
        row = _get(session, article_id)
        if row is None:
            return _bad("Article not found.", 404)
        previous = row.status
        if payload.status == "approved":
            error = _approve(row, user, now, payload.review_due)
            if error:
                return _bad(error)
        else:
            row.status = payload.status
        row.updated_by_id = user.id
        row.updated_at = now
        record_audit(
            session,
            user.id,
            "knowledge.status",
            "knowledge_article",
            row.id,
            {"from": previous, "to": row.status, "review_due": _iso(row.review_due)},
        )
        return _out(row, now.date())


@router.delete(
    f"{BASE}/{{article_id}}",
    summary="Admin: delete a knowledge-base article",
    status_code=204,
)
def delete_article(article_id: int, user: AuthedUser = Depends(get_current_user)):
    _require_admin(user)
    with session_scope(commit=True) as session:
        if not knowledge_available(session):
            return _bad(KNOWLEDGE_UNAVAILABLE)
        row = _get(session, article_id)
        if row is None:
            return _bad("Article not found.", 404)
        record_audit(
            session,
            user.id,
            "knowledge.delete",
            "knowledge_article",
            row.id,
            {"title": row.title, "status": row.status},
        )
        session.delete(row)
        return Response(status_code=204)
