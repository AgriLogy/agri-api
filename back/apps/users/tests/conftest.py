"""Shared fixtures for CustomUser admin tests."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

User = get_user_model()


@pytest.fixture
def admin_user(db):
    u = User.objects.create(
        username="admin1",
        email="admin1@example.com",
        firstname="Admin",
        lastname="One",
        is_active=True,
        is_staff=True,
        is_superuser=False,
    )
    u.set_password("admin-pw")
    u.save()
    return u


@pytest.fixture
def normal_user(db):
    u = User.objects.create(
        username="user1",
        email="user1@example.com",
        firstname="User",
        lastname="One",
        is_active=True,
    )
    u.set_password("user-pw")
    u.save()
    return u


@pytest.fixture
def other_user(db):
    u = User.objects.create(
        username="user2",
        email="user2@example.com",
        firstname="User",
        lastname="Two",
        is_active=True,
    )
    u.set_password("user-pw-2")
    u.save()
    return u


@pytest.fixture
def admin_client(admin_user):
    client = APIClient()
    client.force_authenticate(user=admin_user)
    return client


@pytest.fixture
def user_client(normal_user):
    client = APIClient()
    client.force_authenticate(user=normal_user)
    return client


@pytest.fixture
def anon_client():
    return APIClient()
