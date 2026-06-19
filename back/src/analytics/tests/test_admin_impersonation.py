"""Tests for read-only view-as impersonation (endpoint + middleware)."""

import pytest
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

IMPERSONATE_URL = "/admin/impersonate"


def _readonly_client(user) -> APIClient:
    """An APIClient carrying a read-only impersonation token for ``user``."""
    token = AccessToken.for_user(user)
    token["readonly"] = True
    token["impersonator"] = 1
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


@pytest.mark.django_db
class TestImpersonation:
    def test_normal_user_cannot_impersonate(self, user_bearer, other_user):
        assert (
            user_bearer.post(
                f"{IMPERSONATE_URL}/{other_user.username}", {}, format="json"
            ).status_code
            == 403
        )

    def test_admin_starts_session(self, admin_bearer, normal_user):
        resp = admin_bearer.post(
            f"{IMPERSONATE_URL}/{normal_user.username}", {}, format="json"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["username"] == normal_user.username
        assert body["readonly"] is True
        # The minted token authenticates as the target and is flagged readonly.
        decoded = AccessToken(body["access"])
        assert decoded["user_id"] == normal_user.id
        assert decoded["readonly"] is True

    def test_missing_user_is_404(self, admin_bearer):
        assert (
            admin_bearer.post(
                f"{IMPERSONATE_URL}/ghost-user", {}, format="json"
            ).status_code
            == 404
        )

    def test_readonly_token_blocks_mutations(self, normal_user):
        client = _readonly_client(normal_user)
        # Any non-safe method is rejected by the middleware before routing.
        assert client.post("/alerts", {}, format="json").status_code == 403
        assert client.patch("/users/me", {}, format="json").status_code == 403
        assert client.delete("/alerts/1").status_code == 403

    def test_readonly_token_allows_reads(self, normal_user):
        client = _readonly_client(normal_user)
        # GET passes the middleware (resolves as the target user).
        assert client.get("/sensors").status_code == 200

    def test_readonly_token_cannot_chain_impersonation(self, admin_user):
        # Even a staff user's readonly token cannot POST the impersonate endpoint.
        client = _readonly_client(admin_user)
        assert (
            client.post(
                f"{IMPERSONATE_URL}/{admin_user.username}", {}, format="json"
            ).status_code
            == 403
        )
