"""Shared test fixtures for the django-ninja API surface.

Post-#116 the admin / backoffice endpoints are django-ninja routes that
authenticate via ``JwtAuth(HttpBearer)`` over simplejwt — NOT DRF session
auth. So tests must send ``Authorization: Bearer <access>`` instead of
``force_authenticate``. These fixtures mint a simplejwt access token for the
matching user fixture (``admin_user`` / ``normal_user`` / ``other_user``,
provided by the per-app conftests) and attach it to every request.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient


def _bearer_client(user) -> APIClient:
    from rest_framework_simplejwt.tokens import AccessToken

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
    return client


@pytest.fixture
def admin_bearer(admin_user) -> APIClient:
    """APIClient carrying a Bearer token for a staff user."""
    return _bearer_client(admin_user)


@pytest.fixture
def user_bearer(normal_user) -> APIClient:
    """APIClient carrying a Bearer token for a normal (non-staff) user."""
    return _bearer_client(normal_user)


@pytest.fixture
def other_bearer(other_user) -> APIClient:
    """APIClient carrying a Bearer token for a second normal user."""
    return _bearer_client(other_user)
