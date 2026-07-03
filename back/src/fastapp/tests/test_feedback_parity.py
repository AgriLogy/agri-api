"""F2c golden parity: POST /feedback — fastapp must match the Django ninja
endpoint's contract (response shape, validation, and the row it persists).

The 201 bodies can't be compared byte-for-byte (each insert gets its own
auto-increment id), so parity is asserted on: identical status codes,
identical response *keys*, identical ``status`` value, and — the real proof —
that each surface wrote a ``feedback_bugreport`` row with identical column
values (incl. the metadata→column promotion). The 400 envelope IS compared
byte-for-byte.

``feedback_bugreport`` is agri-db-owned (Django model is managed=False), so
it isn't created by Django migrations — the fixture builds it via
schema_editor, the same way back/conftest.py handles the assistant tables.

Dual-ORM: Postgres only + committed rows (fastapp writes/reads over a
separate SQLAlchemy connection).
"""

from __future__ import annotations

import pytest
from django.conf import settings as dj_settings
from fastapi.testclient import TestClient
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp.main import app
from fastapp.settings import get_settings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM parity requires Postgres",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]

URL = "/feedback"

_META = {
    "url": "https://app.agrogo-datafarm.com/station?zone=3",
    "route": "/station",
    "detected_module": "Station",
    "environment": "production",
    "app_version": "1.82.1",
    "browser": "Google Chrome 140",
    "os": "macOS",
    "viewport": "1440x900",
}


@pytest.fixture(autouse=True)
def _feedback_table(django_db_setup, django_db_blocker):
    """Create the managed=False feedback_bugreport table once (agri-db owns
    it; Django migrations don't build it)."""
    from django.db import connection

    from apps.feedback.models import BugReport

    with django_db_blocker.unblock():
        if BugReport._meta.db_table not in connection.introspection.table_names():
            with connection.schema_editor() as editor:
                editor.create_model(BugReport)
            # Drop the FK to CustomUser: it's managed=False in prod (agri-db
            # owns the real constraint), and its presence breaks the
            # TransactionTestCase teardown (TRUNCATE can't clear the referenced
            # user table). The row-write parity we test needs the columns, not
            # the constraint.
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT conname FROM pg_constraint c "
                    "JOIN pg_class t ON c.conrelid = t.oid "
                    "WHERE t.relname = %s AND c.contype = 'f'",
                    [BugReport._meta.db_table],
                )
                for (conname,) in cur.fetchall():
                    cur.execute(
                        f"ALTER TABLE {BugReport._meta.db_table} "
                        f'DROP CONSTRAINT IF EXISTS "{conname}"'
                    )
    yield


@pytest.fixture(autouse=True)
def _no_network_email(monkeypatch):
    """Force the fastapp Resend key empty so _notify_internal is a no-op in
    tests (Django uses the locmem backend). No mail leaves the process."""
    monkeypatch.setattr(get_settings(), "resend_api_key", "")


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def django() -> APIClient:
    return APIClient()


@pytest.fixture
def reporter(django_user_model):
    return django_user_model.objects.create_user(
        username="fb-reporter",
        email="fb-reporter@example.com",
        password="irrelevant-3921",
    )


def _token(user) -> str:
    return str(AccessToken.for_user(user))


def _row(report_id: int) -> dict:
    """Read a feedback row back as a plain dict of the parity-relevant cols."""
    from apps.feedback.models import BugReport

    r = BugReport.objects.get(id=report_id)
    return {
        "report_type": r.report_type,
        "title": r.title,
        "description": r.description,
        "status": r.status,
        "video_url": r.video_url,
        "user_id": r.user_id,
        "user_email": r.user_email,
        "page_url": r.page_url,
        "route": r.route,
        "module": r.module,
        "environment": r.environment,
        "app_version": r.app_version,
        "browser": r.browser,
        "os": r.os,
        "context": r.context,
    }


def test_submit_response_and_row_match(fast, django, reporter):
    body = {
        "description": "The water chart is blank on the station page.",
        "video_url": "https://res.cloudinary.com/x/v/rec.webm",
        "metadata": _META,
    }
    tok = _token(reporter)

    dj = django.post(URL, body, format="json", HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.post(URL, json=body, headers={"Authorization": f"Bearer {tok}"})

    assert dj.status_code == 201, dj.content
    assert fp.status_code == 201, fp.text

    dj_body, fp_body = dj.json(), fp.json()
    # Same shape + same status value (ids differ by construction).
    assert set(dj_body) == set(fp_body) == {"id", "status"}
    assert dj_body["status"] == fp_body["status"] == "open"
    assert isinstance(dj_body["id"], int) and isinstance(fp_body["id"], int)

    # The real proof: both surfaces persisted identical column values,
    # including the metadata→column promotion + full context blob.
    assert _row(dj_body["id"]) == _row(fp_body["id"])
    written = _row(fp_body["id"])
    assert written["module"] == "Station"
    assert written["page_url"] == _META["url"]
    assert written["user_email"] == "fb-reporter@example.com"
    assert written["context"] == _META


def test_title_defaults_to_description_identically(fast, django, reporter):
    body = {"description": "  short bug, no title  ", "metadata": {}}
    tok = _token(reporter)
    dj = django.post(URL, body, format="json", HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.post(URL, json=body, headers={"Authorization": f"Bearer {tok}"})
    assert dj.status_code == fp.status_code == 201
    dj_row, fp_row = _row(dj.json()["id"]), _row(fp.json()["id"])
    assert dj_row == fp_row
    # trimmed, and title fell back to the description
    assert fp_row["title"] == "short bug, no title"
    assert fp_row["description"] == "short bug, no title"


def test_empty_description_400_is_identical(fast, django, reporter):
    tok = _token(reporter)
    body = {"description": "   "}
    dj = django.post(URL, body, format="json", HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.post(URL, json=body, headers={"Authorization": f"Bearer {tok}"})
    assert dj.status_code == 400
    assert fp.status_code == 400
    assert dj.json() == fp.json() == {"detail": "description is required"}
    assert dj.content == fp.content  # byte-identical 400 envelope


def test_missing_auth_is_401_on_both(fast, django):
    body = {"description": "no token"}
    dj = django.post(URL, body, format="json")
    fp = fast.post(URL, json=body)
    assert dj.status_code == 401
    assert fp.status_code == 401
