import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken


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
