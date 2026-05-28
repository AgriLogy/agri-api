"""Admin user-management router (django-ninja).

Mounted at ``/auth/admin``. Endpoints:

  * GET, POST   /auth/admin/users/
  * GET, PATCH, DELETE  /auth/admin/users/<username>/
  * POST        /auth/admin/users/<username>/activate/
  * POST        /auth/admin/users/<username>/reset-password/

All routes require JWT + ``is_staff`` (checked inline).
"""
from __future__ import annotations

import logging
import secrets
from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, Q
from ninja import Router, Schema
from ninja.responses import Response

from agriBack.api.auth import JwtAuth
from apps.users.models import CustomUser

router = Router()
log = logging.getLogger(__name__)


def _require_admin(request) -> Response | None:
    user = request.auth
    if user is None or not getattr(user, "is_staff", False):
        return Response({"detail": "Admin access required"}, status=403)
    return None


def _annotated(qs):
    return qs.annotate(zones_count=Count("zones", distinct=True))


def _serialize_list(user: CustomUser, zones_count: int | None = None) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "firstname": user.firstname,
        "lastname": user.lastname,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "payement_status": user.payement_status,
        "zones_count": (
            zones_count if zones_count is not None else getattr(user, "zones_count", 0)
        ),
        "date_joined": user.date_joined.isoformat() if user.date_joined else None,
        "last_login": user.last_login.isoformat() if user.last_login else None,
    }


def _serialize_detail(user: CustomUser) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "firstname": user.firstname,
        "lastname": user.lastname,
        "phone_number": user.phone_number,
        "latitude": user.latitude,
        "longitude": user.longitude,
        "payement_status": user.payement_status,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "notify_every": user.notify_every,
        "last_notified": (
            user.last_notified.isoformat() if user.last_notified else None
        ),
        "date_joined": user.date_joined.isoformat() if user.date_joined else None,
        "last_login": user.last_login.isoformat() if user.last_login else None,
        "zones_count": getattr(user, "zones_count", 0),
    }


def _validate_coords(payload: dict) -> dict | None:
    lat = payload.get("latitude")
    lon = payload.get("longitude")
    if lat is not None and (lat < -90 or lat > 90):
        return {"latitude": "Latitude must be between -90 and 90."}
    if lon is not None and (lon < -180 or lon > 180):
        return {"longitude": "Longitude must be between -180 and 180."}
    notify = payload.get("notify_every")
    if notify is not None and (notify < 1 or notify > 168):
        return {"notify_every": "notify_every must be between 1 and 168 hours."}
    return None


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AdminUserCreateIn(Schema):
    username: str
    email: str
    firstname: str
    lastname: str
    phone_number: str
    password: str
    latitude: float | None = None
    longitude: float | None = None
    is_staff: bool = False
    payement_status: str | None = None


class AdminUserUpdateIn(Schema):
    email: str | None = None
    firstname: str | None = None
    lastname: str | None = None
    phone_number: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    payement_status: str | None = None
    is_active: bool | None = None
    is_staff: bool | None = None
    notify_every: int | None = None


class AdminActivateIn(Schema):
    is_active: bool | None = None


class AdminResetPasswordIn(Schema):
    password: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/me",
    auth=JwtAuth(),
    summary="Caller's profile (identity)",
)
def get_me_top(request):
    return {"username": request.auth.username}


@router.post(
    "/me/notifications",
    auth=JwtAuth(),
    summary="Send the caller a field-status notification email",
)
def send_me_notification_top(request):
    from apps.users.router import _send_notification

    return _send_notification(request)


@router.get(
    "",
    auth=JwtAuth(),
    summary="Admin: list all users",
)
def list_users(request, search: str | None = None):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    qs = _annotated(CustomUser.objects.exclude(pk=request.auth.pk))
    if search:
        qs = qs.filter(Q(username__icontains=search) | Q(email__icontains=search))
    qs = qs.order_by("-date_joined")
    return [_serialize_list(u, u.zones_count) for u in qs]


@router.post(
    "",
    auth=JwtAuth(),
    summary="Admin: create a user",
)
def create_user(request, payload: AdminUserCreateIn):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    if CustomUser.objects.filter(username__iexact=payload.username).exists():
        return Response(
            {"username": "This username is already in use."}, status=400,
        )
    if CustomUser.objects.filter(email__iexact=payload.email).exists():
        return Response({"email": "This email is already in use."}, status=400)
    err = _validate_coords(payload.model_dump())
    if err is not None:
        return Response(err, status=400)
    try:
        validate_password(payload.password)
    except DjangoValidationError as exc:
        return Response({"password": list(exc.messages)}, status=400)

    user = CustomUser(
        username=payload.username,
        email=payload.email,
        firstname=payload.firstname,
        lastname=payload.lastname,
        phone_number=payload.phone_number,
        latitude=payload.latitude,
        longitude=payload.longitude,
        is_staff=payload.is_staff,
        payement_status=payload.payement_status,
    )
    user.set_password(payload.password)
    user.save()
    user = _annotated(CustomUser.objects.filter(pk=user.pk)).first()
    return Response(_serialize_detail(user), status=201)


@router.get(
    "/{username}",
    auth=JwtAuth(),
    summary="Admin: fetch one user",
)
def get_user(request, username: str):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    user = _annotated(CustomUser.objects.filter(username=username)).first()
    if user is None:
        return Response({"detail": "User not found."}, status=404)
    return _serialize_detail(user)


@router.patch(
    "/{username}",
    auth=JwtAuth(),
    summary="Admin: patch one user",
)
def patch_user(request, username: str, payload: AdminUserUpdateIn):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    try:
        user = CustomUser.objects.get(username=username)
    except CustomUser.DoesNotExist:
        return Response({"detail": "User not found."}, status=404)
    data = payload.model_dump(exclude_unset=True)
    if "email" in data and data["email"] is not None:
        if (
            CustomUser.objects.exclude(pk=user.pk)
            .filter(email__iexact=data["email"])
            .exists()
        ):
            return Response({"email": "This email is already in use."}, status=400)
    err = _validate_coords(data)
    if err is not None:
        return Response(err, status=400)
    for k, v in data.items():
        setattr(user, k, v)
    user.save()
    user = _annotated(CustomUser.objects.filter(pk=user.pk)).first()
    return _serialize_detail(user)


@router.delete(
    "/{username}",
    auth=JwtAuth(),
    summary="Admin: soft-delete (deactivate) one user",
)
def delete_user(request, username: str):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    try:
        user = CustomUser.objects.get(username=username)
    except CustomUser.DoesNotExist:
        return Response({"detail": "User not found."}, status=404)
    if user.pk == request.auth.pk:
        return Response(
            {"detail": "You cannot delete your own admin account."}, status=400,
        )
    user.is_active = False
    user.save(update_fields=["is_active"])
    return Response(None, status=204)


@router.post(
    "/{username}/activate",
    auth=JwtAuth(),
    summary="Admin: toggle is_active",
)
def activate_user(request, username: str, payload: AdminActivateIn):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    try:
        user = CustomUser.objects.get(username=username)
    except CustomUser.DoesNotExist:
        return Response({"detail": "User not found."}, status=404)
    if payload.is_active is None:
        user.is_active = not user.is_active
    else:
        user.is_active = payload.is_active
    if user.pk == request.auth.pk and not user.is_active:
        return Response(
            {"detail": "You cannot deactivate your own admin account."},
            status=400,
        )
    user.save(update_fields=["is_active"])
    user = _annotated(CustomUser.objects.filter(pk=user.pk)).first()
    return _serialize_detail(user)


@router.post(
    "/{username}/password-reset",
    auth=JwtAuth(),
    summary="Admin: reset a user's password",
)
def reset_password(request, username: str, payload: AdminResetPasswordIn):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    try:
        user = CustomUser.objects.get(username=username)
    except CustomUser.DoesNotExist:
        return Response({"detail": "User not found."}, status=404)
    new_password = payload.password or secrets.token_urlsafe(12)
    try:
        validate_password(new_password, user=user)
    except DjangoValidationError as exc:
        return Response({"password": list(exc.messages)}, status=400)
    user.set_password(new_password)
    user.save(update_fields=["password"])
    log.info("admin %s reset password for %s", request.auth.username, user.username)
    return {"username": user.username, "password": new_password}
