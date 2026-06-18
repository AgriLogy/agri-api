import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken


@pytest.fixture(scope="session", autouse=True)
def _create_assistant_tables(django_db_setup, django_db_blocker):
    """The assistant table is created out-of-band in prod (see
    scripts/ensure_assistant_tables.py); the app has no migrations, so create it
    once for the test DB the same way."""
    from django.db import connection

    from apps.assistant.models import AssistantConversation

    table = AssistantConversation._meta.db_table
    with django_db_blocker.unblock():
        if table not in connection.introspection.table_names():
            with connection.schema_editor() as editor:
                editor.create_model(AssistantConversation)
    yield


@pytest.fixture
def assistant_user(db):
    User = get_user_model()
    return User.objects.create_user(
        username="assistant_tester",
        email="assistant_tester@example.com",
        password="pw-assistant",
    )


@pytest.fixture
def assistant_client(assistant_user) -> APIClient:
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(assistant_user)}"
    )
    return client
