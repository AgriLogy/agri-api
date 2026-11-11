"""Assistant knowledge base search (#461 phase 2).

General agronomy advice must come from articles staff approved in agri-admin,
not from what the language model happens to remember. This module finds the
approved articles relevant to a question; the ``search_knowledge`` tool hands
them to the model, which cites them.

Matching is Postgres full-text search over ``search_vector`` (agri-db
``assistant_knowledge_article``): the query goes through the same
normalisation as the indexed text (:func:`agri.db.assistant.fold_text`),
every remaining word becomes a prefix term (``زيتون:*`` also finds
``زيتونة``), and the terms are OR-ed so a long question still matches an
article that shares only its key words. ``ts_rank_cd`` orders the hits; the
title and keywords weigh more than the body.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

from sqlalchemy import func, select

from agri.db.assistant import (
    KNOWLEDGE_LANGUAGES,
    KNOWLEDGE_TOPICS,
    AssistantKnowledgeArticle,
    fold_text,
)
from fastapp.schema_compat import table_available

KNOWLEDGE_TABLE = AssistantKnowledgeArticle.__tablename__

#: The agri-db revision that creates the table.
KNOWLEDGE_MIGRATION = "b8d4f0a2c3e5"

KNOWLEDGE_UNAVAILABLE = (
    "The assistant knowledge base is not available on this deployment: "
    f"{KNOWLEDGE_TABLE} does not exist. Apply the agri-db migration that "
    f"creates it ({KNOWLEDGE_MIGRATION})."
)

#: Most articles one search returns, and how much of each body the model gets.
MAX_RESULTS = 4
MAX_BODY_CHARS = 2500

# Words that carry no subject on their own, in the languages farmers write.
# Without this an OR query on "comment irriguer la tomate" also ranks every
# article containing "la". Folded like the queries they are compared with.
_STOPWORDS = frozenset(
    fold_text(word)
    for word in """
    le la les un une des du de d l au aux et ou en dans sur pour par avec sans
    est sont quoi quel quelle quels quelles comment quand pourquoi combien
    je tu il elle on nous vous ils elles mon ma mes ton ta tes son sa ses ce
    cette ces qui que qu ne pas plus faut faire fait dois doit peux peut
    the a an of to in on for with is are what how when why which my your
    do does can should i you it this that and or
    واش اش شنو شنوا شنا كيفاش علاش فين امتا شحال اشمن واحد
    فى في من على الى عن مع هل ما ماذا كيف لماذا متى اين هذا هذه ذلك التى التي الذي
    انا نتا نتي هو هي حنا نتوما هوما ديال ديالي ديالك ليا ليك بغيت خاصني خاص
    و ف ب ك ل ولا او
    wach chno chnou kifach kifash 3lach fin imta ch7al dyal dyali dial
    ana nta nti howa hiya 7na bghit khasni khass
    """.split()
)

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_MAX_TERMS = 12


def query_terms(text: str) -> list[str]:
    """The searchable words of ``text``: normalised like the index, filler
    words and single characters dropped, de-duplicated, at most 12."""
    terms: list[str] = []
    for word in _WORD.findall(fold_text(text or "")):
        if len(word) < 2 or word in _STOPWORDS or word in terms:
            continue
        terms.append(word)
        if len(terms) == _MAX_TERMS:
            break
    return terms


def to_tsquery_text(terms: list[str]) -> str:
    """``['sqi', 'زيتون']`` → ``sqi:* | زيتون:*``. Terms are word characters
    only (see :data:`_WORD`), so no tsquery operator can be injected."""
    return " | ".join(f"{t}:*" for t in terms)


def knowledge_available(session: Any) -> bool:
    return table_available(session, KNOWLEDGE_TABLE)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def article_for_assistant(
    row: AssistantKnowledgeArticle, *, today: datetime.date
) -> dict[str, Any]:
    """What the model sees of one article (and what the app shows as source)."""
    body = row.body or ""
    truncated = len(body) > MAX_BODY_CHARS
    return {
        "id": row.id,
        "title": row.title,
        "topic": row.topic,
        "language": row.language,
        "crops": list(row.crops or []),
        "content": body[:MAX_BODY_CHARS] + (" […]" if truncated else ""),
        "sources": list(row.sources or []),
        "reviewed_at": _iso(row.reviewed_at),
        "review_due": _iso(row.review_due),
        "review_overdue": bool(row.review_due and row.review_due < today),
    }


def search_articles(
    session: Any,
    query: str,
    *,
    topic: str | None = None,
    crop: str | None = None,
    limit: int = MAX_RESULTS,
    today: datetime.date | None = None,
) -> list[dict[str, Any]]:
    """Approved articles matching ``query``, best first.

    ``topic`` narrows to one subject. ``crop`` keeps articles written for
    that crop or for no crop in particular (empty ``crops``). Archived,
    draft and in-review articles are never returned.
    """
    terms = query_terms(query)
    if not terms:
        return []
    tsquery = func.to_tsquery("simple", to_tsquery_text(terms))
    rank = func.ts_rank_cd(AssistantKnowledgeArticle.search_vector, tsquery)
    stmt = (
        select(AssistantKnowledgeArticle)
        .where(AssistantKnowledgeArticle.status == "approved")
        .where(AssistantKnowledgeArticle.search_vector.op("@@")(tsquery))
        .order_by(rank.desc(), AssistantKnowledgeArticle.id)
    )
    if topic in KNOWLEDGE_TOPICS:
        stmt = stmt.where(AssistantKnowledgeArticle.topic == topic)
    rows = list(session.execute(stmt).scalars())
    if crop:
        wanted = fold_text(crop).strip()
        rows = [
            r
            for r in rows
            if not r.crops
            or any(wanted and wanted in fold_text(str(c)) for c in r.crops)
        ]
    today = today or datetime.date.today()
    return [article_for_assistant(r, today=today) for r in rows[: max(1, limit)]]


__all__ = [
    "KNOWLEDGE_LANGUAGES",
    "KNOWLEDGE_MIGRATION",
    "KNOWLEDGE_TABLE",
    "KNOWLEDGE_TOPICS",
    "KNOWLEDGE_UNAVAILABLE",
    "article_for_assistant",
    "knowledge_available",
    "query_terms",
    "search_articles",
    "to_tsquery_text",
]
