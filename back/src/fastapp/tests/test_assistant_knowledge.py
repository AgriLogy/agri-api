"""Assistant knowledge base (#461 phase 2): admin workflow, search tool, chat.

Postgres only: the table's ``search_vector`` is a generated tsvector, created
by conftest from the agri.db definition (the real schema, not a mirror).
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.conf import settings as dj_settings
from django.db import connection
from fastapi.testclient import TestClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp.assistant import config as llm_config
from fastapp.assistant import knowledge as knowledge_mod
from fastapp.assistant import providers as providers_mod
from fastapp.assistant.prompt import SYSTEM_PROMPT
from fastapp.assistant.providers import LLMResult, ToolCallRecord
from fastapp.main import app
from fastapp.routers import admin_knowledge
from fastapp.settings import get_settings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="tsvector search + dual-ORM: needs Postgres",
)
pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]

KB = "/admin/assistant/knowledge"
TOOL = "/assistant/tools/search_knowledge"
CHAT = "/assistant/chat"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)
    monkeypatch.setattr(get_settings(), "ai_api_key", "")


@pytest.fixture(autouse=True)
def _clean():
    """The table has no Django model, so test flushes never empty it."""
    yield
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM assistant_knowledge_article")
        cursor.execute(
            "DELETE FROM analytics_systemsetting WHERE key = %s",
            [llm_config.SETTING_KEY],
        )


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin(django_user_model, set_access_level):
    user = django_user_model.objects.create_user(
        username="kb-admin", email="kb-admin@example.com", password="irrelevant-3921"
    )
    set_access_level(user, "admin")
    return user


@pytest.fixture
def farmer(django_user_model):
    return django_user_model.objects.create_user(
        username="kb-farmer", email="kb-farmer@example.com", password="irrelevant-3921"
    )


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {AccessToken.for_user(user)}"}


SOURCE = {"title": "FAO-56", "url": "https://www.fao.org/3/x0490e/x0490e00.htm"}


def _article(**over) -> dict:
    body = {
        "title": "Irrigation de l'olivier en été",
        "language": "fr",
        "topic": "irrigation",
        "body": "En été, l'olivier en goutte-à-goutte a besoin d'apports réguliers.",
        "keywords": "سقي زيتون sqi zitoun",
        "crops": ["olivier"],
        "sources": [SOURCE],
    }
    body.update(over)
    return body


def _create(fast, admin, **over) -> dict:
    res = fast.post(KB, json=_article(**over), headers=_auth(admin))
    assert res.status_code == 201, res.text
    return res.json()


def _approve(fast, admin, article_id: int, **extra) -> dict:
    res = fast.post(
        f"{KB}/{article_id}/status",
        json={"status": "approved", **extra},
        headers=_auth(admin),
    )
    assert res.status_code == 200, res.text
    return res.json()


def _search(fast, user, **params) -> dict:
    res = fast.post(TOOL, json={"params": params}, headers=_auth(user))
    assert res.status_code == 200, res.text
    return res.json()["data"]


# ===========================================================================
# Admin workflow
# ===========================================================================
def test_article_lifecycle(fast, admin):
    created = _create(fast, admin)
    assert created["status"] == "draft"
    assert created["created_by_id"] == admin.id
    assert created["reviewed_at"] is None

    approved = _approve(fast, admin, created["id"])
    assert approved["status"] == "approved"
    assert approved["reviewed_by_id"] == admin.id
    # No review date given: one year out.
    due = dt.date.fromisoformat(approved["review_due"])
    assert due - dt.date.today() == admin_knowledge.DEFAULT_REVIEW_PERIOD

    listing = fast.get(KB, headers=_auth(admin)).json()
    assert listing["available"] is True
    assert listing["counts"]["approved"] == 1
    assert [a["id"] for a in listing["items"]] == [created["id"]]
    assert "crop_problems" in listing["topics"]

    # Editing the text of an approved article takes it off the assistant.
    edited = fast.put(
        f"{KB}/{created['id']}",
        json=_article(body="Texte corrigé.", review_due=approved["review_due"]),
        headers=_auth(admin),
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "review"

    assert fast.delete(f"{KB}/{created['id']}", headers=_auth(admin)).status_code == 204
    assert fast.get(f"{KB}/{created['id']}", headers=_auth(admin)).status_code == 404


def test_a_review_date_only_change_keeps_the_approval(fast, admin):
    created = _create(fast, admin)
    _approve(fast, admin, created["id"])
    later = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    res = fast.put(
        f"{KB}/{created['id']}",
        json=_article(review_due=later),
        headers=_auth(admin),
    )
    assert res.json()["status"] == "approved"
    assert res.json()["review_due"] == later


def test_approval_needs_a_source_and_a_future_review_date(fast, admin):
    created = _create(fast, admin, sources=[])
    res = fast.post(
        f"{KB}/{created['id']}/status",
        json={"status": "approved"},
        headers=_auth(admin),
    )
    assert res.status_code == 400
    assert "source" in res.json()["detail"]

    sourced = _create(fast, admin)
    past = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    res = fast.post(
        f"{KB}/{sourced['id']}/status",
        json={"status": "approved", "review_due": past},
        headers=_auth(admin),
    )
    assert res.status_code == 400


@pytest.mark.parametrize(
    "over",
    [
        {"language": "es"},
        {"topic": "astrology"},
        {"status": "approved"},
        {"sources": [{"title": "x", "url": "javascript:alert(1)"}]},
        {"body": "   "},
    ],
)
def test_create_validation(fast, admin, over):
    res = fast.post(KB, json=_article(**over), headers=_auth(admin))
    assert res.status_code in (400, 422), res.text


def test_knowledge_admin_is_admin_only(fast, farmer):
    assert fast.get(KB, headers=_auth(farmer)).status_code == 403
    assert fast.post(KB, json=_article(), headers=_auth(farmer)).status_code == 403


def test_missing_table_is_reported_not_a_500(fast, admin, monkeypatch):
    monkeypatch.setattr(admin_knowledge, "knowledge_available", lambda s: False)
    listing = fast.get(KB, headers=_auth(admin)).json()
    assert listing["available"] is False
    assert knowledge_mod.KNOWLEDGE_MIGRATION in listing["detail"]
    res = fast.post(KB, json=_article(), headers=_auth(admin))
    assert res.status_code == 400
    assert knowledge_mod.KNOWLEDGE_MIGRATION in res.json()["detail"]


# ===========================================================================
# search_knowledge tool
# ===========================================================================
def test_only_approved_articles_are_searchable(fast, admin, farmer):
    approved = _create(fast, admin)
    _approve(fast, admin, approved["id"])
    _create(fast, admin, title="Brouillon olivier", status="review")

    data = _search(fast, farmer, query="olivier irrigation")
    assert data["available"] is True
    assert [a["id"] for a in data["articles"]] == [approved["id"]]
    (article,) = data["articles"]
    assert article["sources"] == [SOURCE]
    assert article["review_overdue"] is False
    assert article["content"].startswith("En été")


def test_darija_question_finds_the_article_through_keywords_and_clitics(
    fast, admin, farmer
):
    fr = _create(fast, admin)
    _approve(fast, admin, fr["id"])
    ary = _create(
        fast,
        admin,
        title="السقي بالتنقيط للزيتون",
        language="ary",
        body="فالصيف خاص الأرض تبقى رطبة.",
        keywords="",
    )
    _approve(fast, admin, ary["id"])
    _create(fast, admin, title="Tomate sous serre", body="Serre.", keywords="")

    # "الزيتون" with its article; "الأرض" with hamza; Arabizi keyword.
    found = {
        a["id"] for a in _search(fast, farmer, query="واش نسقي الزيتون")["articles"]
    }
    assert found == {fr["id"], ary["id"]}
    by_hamza = _search(fast, farmer, query="ارض")["articles"]
    assert [a["id"] for a in by_hamza] == [ary["id"]]
    by_arabizi = _search(fast, farmer, query="wach nsqi zitoun")["articles"]
    assert [a["id"] for a in by_arabizi] == [fr["id"]]


def test_title_and_keywords_outrank_the_body(fast, admin, farmer):
    in_body = _create(fast, admin, title="Généralités", keywords="", body="mildiou")
    in_title = _create(fast, admin, title="Le mildiou", keywords="", body="Maladie.")
    for a in (in_body, in_title):
        _approve(fast, admin, a["id"])
    ids = [a["id"] for a in _search(fast, farmer, query="mildiou")["articles"]]
    assert ids == [in_title["id"], in_body["id"]]


def test_topic_and_crop_filters(fast, admin, farmer):
    olive = _create(fast, admin)
    general = _create(fast, admin, title="Irrigation : principes", crops=[])
    tomato = _create(fast, admin, title="Irrigation tomate", crops=["tomate"])
    soil = _create(fast, admin, title="Irrigation et sol", topic="soil", crops=[])
    for a in (olive, general, tomato, soil):
        _approve(fast, admin, a["id"])

    olive_hits = {
        a["id"]
        for a in _search(fast, farmer, query="irrigation", crop="olivier")["articles"]
    }
    assert tomato["id"] not in olive_hits
    assert {olive["id"], general["id"]} <= olive_hits
    soil_only = _search(fast, farmer, query="irrigation", topic="soil")["articles"]
    assert [a["id"] for a in soil_only] == [soil["id"]]


def test_overdue_articles_are_flagged(fast, admin, farmer):
    created = _create(fast, admin)
    _approve(fast, admin, created["id"])
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE assistant_knowledge_article SET review_due = %s WHERE id = %s",
            [dt.date.today() - dt.timedelta(days=3), created["id"]],
        )
    (article,) = _search(fast, farmer, query="olivier")["articles"]
    assert article["review_overdue"] is True


def test_empty_query_and_missing_table(fast, farmer, monkeypatch):
    assert _search(fast, farmer, query="  ")["error"]
    monkeypatch.setattr(knowledge_mod, "knowledge_available", lambda s: False)
    data = _search(fast, farmer, query="olivier")
    assert data == {
        "available": False,
        "articles": [],
        "error": "The knowledge base is not available yet.",
    }


# ===========================================================================
# Chat: the model is told to use it, and the answer cites the articles
# ===========================================================================
class _SearchingProvider:
    def __init__(self):
        self.seen: dict = {}

    def complete(self, *, system, context, history, message, tools, execute):
        self.seen = {"system": system, "tools": {t.name: t for t in tools}}
        out = execute("search_knowledge", {"query": "olivier irrigation"})
        return LLMResult(
            text="Selon l'article « Irrigation de l'olivier en été »…",
            tool_calls=[ToolCallRecord("search_knowledge", {"query": "olivier"}, out)],
            model="fake-model",
        )


def test_chat_cites_knowledge_articles(fast, admin, farmer, monkeypatch):
    created = _create(fast, admin)
    _approve(fast, admin, created["id"])
    saved = fast.put(
        "/admin/assistant/config",
        json={
            "enabled": True,
            "provider": "anthropic",
            "model": "claude-haiku-5-5",
            "api_key": "sk-ant-test-1234",
        },
        headers=_auth(admin),
    )
    assert saved.status_code == 200, saved.text
    provider = _SearchingProvider()
    monkeypatch.setattr(providers_mod, "build_provider", lambda cfg: provider)

    res = fast.post(
        CHAT, json={"message": "Comment irriguer mes oliviers ?"}, headers=_auth(farmer)
    )
    assert res.status_code == 200, res.text
    (source,) = res.json()["sources"]
    assert source["tool"] == "search_knowledge"
    (cited,) = source["articles"]
    assert cited["id"] == created["id"]
    assert cited["title"] == "Irrigation de l'olivier en été"
    assert cited["sources"] == [SOURCE]

    assert "# Knowledge base" in provider.seen["system"]
    spec = provider.seen["tools"]["search_knowledge"]
    assert spec.parameters["required"] == ["query"]
    assert "crop_problems" in spec.parameters["properties"]["topic"]["enum"]


def test_knowledge_rules_are_in_the_stable_prompt():
    assert "not from the Agrogo knowledge base" in SYSTEM_PROMPT


# ===========================================================================
# Generic /admin/db editor: the generated search column is read-only
# ===========================================================================
def test_admin_db_never_writes_the_generated_search_column(
    fast, admin, django_user_model
):
    staff = django_user_model.objects.create_user(
        username="kb-staff",
        email="kb-staff@example.com",
        password="irrelevant-3921",
        is_staff=True,
    )
    key = "assistant.knowledgearticle"
    schema = fast.get(f"/admin/db/tables/{key}/schema", headers=_auth(staff))
    assert schema.status_code == 200, schema.text
    fields = {f["name"]: f for f in schema.json()["fields"]}
    assert fields["search_vector"]["editable"] is False
    assert fields["search_vector"]["required"] is False

    created = _create(fast, admin)
    res = fast.patch(
        f"/admin/db/tables/{key}/rows/{created['id']}",
        json={"title": "Olivier : irrigation d'été", "search_vector": "'x':1"},
        headers=_auth(staff),
    )
    assert res.status_code == 200, res.text
    # The edit went through and Postgres recomputed the index from it.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT search_vector @@ to_tsquery('simple', 'ete:*') "
            "FROM assistant_knowledge_article WHERE id = %s",
            [created["id"]],
        )
        assert cursor.fetchone()[0] is True
