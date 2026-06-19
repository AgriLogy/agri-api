"""Admin system-settings router (django-ninja) — mounted at ``/admin/settings``.

  * GET    /admin/settings        → all settings grouped by category (seeds defaults)
  * PATCH  /admin/settings        → upsert a {key: value} map
  * POST   /admin/settings        → create a new key (category + value)
  * DELETE /admin/settings/{key}  → delete a key

All routes require JWT + ``is_staff``.
"""

from __future__ import annotations

from typing import Any

from ninja import Router, Schema
from ninja.responses import Response

from agriapi.api.auth import JwtAuth
from apps.irrigation.audit import record_audit
from apps.irrigation.models import SystemSetting
from apps.irrigation.router_admin import _require_admin

router = Router()

# Sensible defaults, seeded lazily on first GET.
DEFAULT_SETTINGS: list[dict[str, Any]] = [
    {
        "key": "support_email",
        "category": "general",
        "value": "contact.agrilogy@gmail.com",
    },
    {"key": "notify_email_enabled", "category": "notifications", "value": True},
    {"key": "notify_sms_enabled", "category": "notifications", "value": False},
    {"key": "notify_whatsapp_enabled", "category": "notifications", "value": False},
    {"key": "default_critical_moisture_pct", "category": "thresholds", "value": 20},
    {"key": "stale_device_hours", "category": "thresholds", "value": 24},
]


class SettingsPatchIn(Schema):
    values: dict[str, Any] = {}


class SettingCreateIn(Schema):
    key: str
    value: Any = ""
    category: str = "general"


def _seed_defaults() -> None:
    existing = set(SystemSetting.objects.values_list("key", flat=True))
    for d in DEFAULT_SETTINGS:
        if d["key"] not in existing:
            SystemSetting.objects.create(
                key=d["key"], value=d["value"], category=d["category"]
            )


def _grouped() -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for s in SystemSetting.objects.all().order_by("category", "key"):
        out.setdefault(s.category or "general", []).append(
            {"key": s.key, "value": s.value}
        )
    return out


@router.get("", auth=JwtAuth(), summary="Admin: get system settings")
def get_settings(request):
    guard = _require_admin(request)
    if guard:
        return guard
    _seed_defaults()
    return _grouped()


@router.patch("", auth=JwtAuth(), summary="Admin: update system settings")
def patch_settings(request, payload: SettingsPatchIn):
    guard = _require_admin(request)
    if guard:
        return guard
    for key, value in (payload.values or {}).items():
        SystemSetting.objects.update_or_create(key=key, defaults={"value": value})
    record_audit(request.auth, "settings.update", "settings", "", payload.values or {})
    return _grouped()


@router.post("", auth=JwtAuth(), summary="Admin: create a system setting key")
def create_setting(request, payload: SettingCreateIn):
    guard = _require_admin(request)
    if guard:
        return guard
    key = (payload.key or "").strip()
    if not key:
        return Response({"detail": "key is required"}, status=400)
    if SystemSetting.objects.filter(key=key).exists():
        return Response({"detail": "Setting already exists"}, status=409)
    SystemSetting.objects.create(
        key=key, value=payload.value, category=(payload.category or "general")
    )
    record_audit(
        request.auth,
        "settings.create",
        "settings",
        key,
        {"category": payload.category, "value": payload.value},
    )
    return _grouped()


@router.delete("/{key}", auth=JwtAuth(), summary="Admin: delete a system setting key")
def delete_setting(request, key: str):
    guard = _require_admin(request)
    if guard:
        return guard
    deleted, _ = SystemSetting.objects.filter(key=key).delete()
    if not deleted:
        return Response({"detail": "Not found"}, status=404)
    record_audit(request.auth, "settings.delete", "settings", key)
    return _grouped()
