"""Smoke for the v2 ``GET /api/v2/sensors/keys`` endpoint.

Locks in the django-ninja + JWT auth wiring + the response shape.
The exhaustive registry-content tests live in agri-core's
``tests/test_alerts.py``; this file only verifies the HTTP surface.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from agri.core.alerts import SENSOR_KEY_REGISTRY


class SensorKeysEndpointTests(TestCase):
    URL = "/api/v2/sensors/keys"

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="v2-tester", password="pw", email="v2@example.test",
        )
        self.client = Client()

    def _token(self) -> str:
        resp = self.client.post(
            "/auth/token/",
            data={"username": "v2-tester", "password": "pw"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()["access"]

    def test_requires_auth(self):
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 401)

    def test_returns_full_registry_when_authenticated(self):
        resp = self.client.get(
            self.URL, HTTP_AUTHORIZATION=f"Bearer {self._token()}",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("items", body)
        self.assertEqual(len(body["items"]), len(SENSOR_KEY_REGISTRY))
        sample = body["items"][0]
        for field in ("key", "label", "unit", "type"):
            self.assertIn(field, sample)
