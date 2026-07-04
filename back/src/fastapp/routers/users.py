"""fastapp /users admin surface + /users/me/notifications (strangler F5c).

Byte-parity port of ``apps/users/router_admin.py`` (django-ninja, mounted at
``/users``): the user-management admin console (list / create / fetch / patch /
soft-delete / activate / password-reset / session-status / force-logout) plus
the caller's on-demand notification email (``POST /users/me/notifications``).

NOTE on the ``/users`` prefix split (see main.py / nginx):

  * ``GET``/``PATCH`` ``/users/me`` stay with ``selfreads`` (already live).
  * ``/users/{username}/{zones,alerts,activity,sensor-units,zones/*}`` stay with
    ``admin_analytics``.
  * THIS router owns ``/users`` (list/create), ``/users/{username}`` (exact
    GET/PATCH/DELETE), the ``/users/{username}/{activate,password-reset,
    sessions,force-logout}`` admin ops, and ``/users/me/notifications``.

Data access is SQLAlchemy via agri-core (no Django ORM). The admin guard mirrors
the Django router's inline ``_require_admin`` (403 body ``{"detail": "Admin
access required"}`` — deliberately NOT ``get_current_staff_user``'s message).
Password hashing/validation reuse ``fastapp.passwords`` (Django-compatible
pbkdf2 + validator messages); the password-reset path additionally reproduces
Django's ``UserAttributeSimilarityValidator`` because that route calls
``validate_password(new_password, user=user)``.
"""

from __future__ import annotations

import datetime
import logging
import re
import secrets
from difflib import SequenceMatcher
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from agri.core.database import session_scope
from agri.db.analytics import AnalyticsZone
from agri.db.users import CustomUserCustomuser
from fastapp import email
from fastapp.auth import AuthedUser, get_current_user
from fastapp.json import DjangoStyleJSONResponse
from fastapp.passwords import make_password, validate_password
from fastapp.settings import get_settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["users"])

# CustomUser.LANGUAGE_CHOICES keys, verbatim (apps/users/models.py).
_LANGUAGE_CHOICES = {"fr", "ar"}

# SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"] (agriapi/settings/base.py), replicated so
# the sidecar computes the same token-expiry as ``simplejwt_settings`` without
# importing Django.
_ACCESS_TOKEN_LIFETIME = datetime.timedelta(days=5)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _admin_guard(user: AuthedUser):
    """Mirror the Django router's ``_require_admin``: a 403 body that reads
    ``{"detail": "Admin access required"}`` (NOT get_current_staff_user's
    generic message), so the cutover is byte-identical for non-staff too."""
    if not user.is_staff:
        return DjangoStyleJSONResponse(
            {"detail": "Admin access required"}, status_code=403
        )
    return None


# ---------------------------------------------------------------------------
# Serialization (field order matches the Django serializers exactly)
# ---------------------------------------------------------------------------
def _serialize_list(user: CustomUserCustomuser, zones_count: int) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "firstname": user.firstname,
        "lastname": user.lastname,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "payement_status": user.payement_status,
        "zones_count": zones_count,
        "date_joined": user.date_joined.isoformat() if user.date_joined else None,
        "last_login": user.last_login.isoformat() if user.last_login else None,
    }


def _serialize_detail(user: CustomUserCustomuser, zones_count: int) -> dict[str, Any]:
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
        "preferred_language": user.preferred_language,
        "last_notified": (
            user.last_notified.isoformat() if user.last_notified else None
        ),
        "date_joined": user.date_joined.isoformat() if user.date_joined else None,
        "last_login": user.last_login.isoformat() if user.last_login else None,
        "sessions_revoked_at": (
            user.sessions_revoked_at.isoformat() if user.sessions_revoked_at else None
        ),
        "zones_count": zones_count,
    }


def _serialize_sessions(user: CustomUserCustomuser) -> dict[str, Any]:
    """Derived auth-token status — a straight port of the Django helper."""
    last_login = user.last_login
    revoked_at = user.sessions_revoked_at
    access_lifetime = _ACCESS_TOKEN_LIFETIME

    revoked = revoked_at is not None and (
        last_login is None or revoked_at >= last_login
    )

    if not user.is_active:
        status = "disabled"
    elif revoked:
        status = "revoked"
    elif last_login is not None:
        status = "active"
    else:
        status = "never_logged_in"

    expires_at = None
    if status == "active":
        expires_at = (last_login + access_lifetime).isoformat()

    return {
        "username": user.username,
        "is_active": user.is_active,
        "status": status,
        "last_login": last_login.isoformat() if last_login else None,
        "sessions_revoked_at": revoked_at.isoformat() if revoked_at else None,
        "access_token_lifetime_minutes": int(access_lifetime.total_seconds() // 60),
        "current_token_expires_at": expires_at,
    }


def _validate_coords(payload: dict) -> dict | None:
    lat = payload.get("latitude")
    lon = payload.get("longitude")
    if lat is not None and (lat < -90 or lat > 90):
        return {"latitude": "Latitude must be between -90 and 90."}
    if lon is not None and (lon < -180 or lon > 180):
        return {"longitude": "Longitude must be between -180 and 180."}
    notify = payload.get("notify_every")
    if notify is not None and (notify < 10 or notify > 10080):
        return {"notify_every": "notify_every must be between 10 and 10080 minutes."}
    return None


# ---------------------------------------------------------------------------
# Password validation (Django parity)
# ---------------------------------------------------------------------------
# UserAttributeSimilarityValidator defaults (django.contrib.auth). CustomUser
# has ``username``/``email`` but no ``first_name``/``last_name`` attributes, so
# only those two are ever compared; their verbose names are the field names.
_PW_SIMILARITY_MAX = 0.7
_USER_ATTRIBUTES = ("username", "first_name", "last_name", "email")
_ATTR_VERBOSE_NAMES = {"username": "username", "email": "email"}


def _exceeds_maximum_length_ratio(password: str, max_similarity: float, value: str):
    """Verbatim port of django...password_validation.exceeds_maximum_length_ratio."""
    pwd_len = len(password)
    length_bound_similarity = max_similarity / 2 * pwd_len
    value_len = len(value)
    return pwd_len >= 10 * value_len and value_len < length_bound_similarity


def _password_too_similar(password: str, attrs: dict[str, str | None]) -> list[str]:
    """Port of UserAttributeSimilarityValidator.validate: raise (→ one message)
    on the first attribute/part the password is >= 0.7 similar to."""
    pw = password.lower()
    for name in _USER_ATTRIBUTES:
        value = attrs.get(name)
        if not value or not isinstance(value, str):
            continue
        value_lower = value.lower()
        value_parts = re.split(r"\W+", value_lower) + [value_lower]
        for part in value_parts:
            if _exceeds_maximum_length_ratio(pw, _PW_SIMILARITY_MAX, part):
                continue
            if SequenceMatcher(a=pw, b=part).quick_ratio() >= _PW_SIMILARITY_MAX:
                vn = _ATTR_VERBOSE_NAMES.get(name, name)
                return [f"The password is too similar to the {vn}."]
    return []


def _validate_password_with_user(password: str, username: str, email: str):
    """django.contrib.auth.password_validation.validate_password(password, user)
    message order: UserAttributeSimilarity, MinimumLength, CommonPassword,
    NumericPassword (AUTH_PASSWORD_VALIDATORS order in base.py)."""
    return _password_too_similar(
        password, {"username": username, "email": email}
    ) + validate_password(password)


# ---------------------------------------------------------------------------
# User resolution + zone-count helpers
# ---------------------------------------------------------------------------
def _resolve_user(session, username: str) -> CustomUserCustomuser | None:
    return session.scalars(
        select(CustomUserCustomuser).where(CustomUserCustomuser.username == username)
    ).first()


def _zones_count(session, user_id: int) -> int:
    return (
        session.scalar(
            select(func.count(AnalyticsZone.id)).where(AnalyticsZone.user_id == user_id)
        )
        or 0
    )


def _zones_count_map(session, user_ids: list[int]) -> dict[int, int]:
    if not user_ids:
        return {}
    rows = session.execute(
        select(AnalyticsZone.user_id, func.count(AnalyticsZone.id))
        .where(AnalyticsZone.user_id.in_(user_ids))
        .group_by(AnalyticsZone.user_id)
    ).all()
    return {uid: cnt for uid, cnt in rows}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class AdminUserCreateIn(BaseModel):
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


class AdminUserUpdateIn(BaseModel):
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
    preferred_language: str | None = None


class AdminActivateIn(BaseModel):
    is_active: bool | None = None


class AdminResetPasswordIn(BaseModel):
    password: str | None = None


# ---------------------------------------------------------------------------
# /users/me/notifications
# ---------------------------------------------------------------------------
@router.post(
    "/users/me/notifications",
    summary="Send the caller a field-status notification email",
)
def send_me_notification(user: AuthedUser = Depends(get_current_user)):
    """Port of ``_send_notification`` (apps/users/router.py): compose the French
    field-status body via agri-core, send it, stamp ``last_notified``. Email
    goes over the Resend HTTPS client (same transport the Django prod backend
    uses); a provider rejection maps to the Django 500 envelope (Django's
    ``send_mail(fail_silently=False)`` raises on failure)."""
    from agri.core.notifications import compose_notification_for_user

    recipient = (user.email or "").strip()
    if not recipient:
        return DjangoStyleJSONResponse(
            {"success": False, "error": "user has no email address on file"},
            status_code=400,
        )
    settings = get_settings()
    try:
        with session_scope(commit=True) as session:
            body = compose_notification_for_user(session, user.id, now=_utcnow())
            if body is None:
                body = ""
            sent = email.send_email(
                api_key=settings.resend_api_key,
                from_email=settings.default_from_email,
                to=[recipient],
                subject="Mise à jour de votre terrain agricole",
                text=body,
            )
            if not sent:
                raise RuntimeError("notification email not accepted by provider")
            row = session.get(CustomUserCustomuser, user.id)
            if row is not None:
                row.last_notified = _utcnow()
            session.flush()
    except Exception:
        log.exception("Failed to send notification email to %s", recipient)
        return DjangoStyleJSONResponse(
            {"success": False, "error": "Failed to send notification email."},
            status_code=500,
        )
    log.info("Sent on-demand notification email to %s", recipient)
    return {"success": True, "message": "Email envoyé avec succès."}


# ---------------------------------------------------------------------------
# /users  (admin list + create)
# ---------------------------------------------------------------------------
@router.get("/users", summary="Admin: list all users")
def list_users(search: str | None = None, user: AuthedUser = Depends(get_current_user)):
    guard = _admin_guard(user)
    if guard is not None:
        return guard
    with session_scope() as session:
        stmt = select(CustomUserCustomuser).where(CustomUserCustomuser.id != user.id)
        if search:
            like = f"%{search}%"
            stmt = stmt.where(
                CustomUserCustomuser.username.ilike(like)
                | CustomUserCustomuser.email.ilike(like)
            )
        # id.desc() is a deterministic tie-break so users sharing an identical
        # date_joined come out in the same order as the Django surface.
        stmt = stmt.order_by(
            CustomUserCustomuser.date_joined.desc(), CustomUserCustomuser.id.desc()
        )
        rows = session.scalars(stmt).all()
        counts = _zones_count_map(session, [u.id for u in rows])
        return [_serialize_list(u, counts.get(u.id, 0)) for u in rows]


@router.post("/users", summary="Admin: create a user")
def create_user(
    payload: AdminUserCreateIn, user: AuthedUser = Depends(get_current_user)
):
    guard = _admin_guard(user)
    if guard is not None:
        return guard
    with session_scope(commit=True) as session:
        if session.scalar(
            select(func.count(CustomUserCustomuser.id)).where(
                func.lower(CustomUserCustomuser.username) == payload.username.lower()
            )
        ):
            return DjangoStyleJSONResponse(
                {"username": "This username is already in use."}, status_code=400
            )
        if session.scalar(
            select(func.count(CustomUserCustomuser.id)).where(
                func.lower(CustomUserCustomuser.email) == payload.email.lower()
            )
        ):
            return DjangoStyleJSONResponse(
                {"email": "This email is already in use."}, status_code=400
            )
        err = _validate_coords(payload.model_dump())
        if err is not None:
            return DjangoStyleJSONResponse(err, status_code=400)
        pw_errors = validate_password(payload.password)
        if pw_errors:
            return DjangoStyleJSONResponse({"password": pw_errors}, status_code=400)

        new_user = CustomUserCustomuser(
            username=payload.username,
            email=payload.email,
            firstname=payload.firstname,
            lastname=payload.lastname,
            phone_number=payload.phone_number,
            latitude=payload.latitude,
            longitude=payload.longitude,
            is_staff=payload.is_staff,
            # NOT NULL columns with Django app-level defaults (no DB default) —
            # supply them explicitly to mirror the Django model.
            payement_status=payload.payement_status or "actif",
            is_active=True,
            is_superuser=False,
            is_technician=False,
            notify_every=240,
            preferred_language="fr",
            date_joined=_utcnow(),
            password=make_password(payload.password),
        )
        session.add(new_user)
        session.flush()
        return DjangoStyleJSONResponse(_serialize_detail(new_user, 0), status_code=201)


# ---------------------------------------------------------------------------
# /users/{username}  (admin fetch / patch / soft-delete)
# ---------------------------------------------------------------------------
@router.get("/users/{username}", summary="Admin: fetch one user")
def get_user(username: str, user: AuthedUser = Depends(get_current_user)):
    guard = _admin_guard(user)
    if guard is not None:
        return guard
    with session_scope() as session:
        row = _resolve_user(session, username)
        if row is None:
            return DjangoStyleJSONResponse(
                {"detail": "User not found."}, status_code=404
            )
        return _serialize_detail(row, _zones_count(session, row.id))


@router.patch("/users/{username}", summary="Admin: patch one user")
def patch_user(
    username: str,
    payload: AdminUserUpdateIn,
    user: AuthedUser = Depends(get_current_user),
):
    guard = _admin_guard(user)
    if guard is not None:
        return guard
    with session_scope(commit=True) as session:
        row = _resolve_user(session, username)
        if row is None:
            return DjangoStyleJSONResponse(
                {"detail": "User not found."}, status_code=404
            )
        data = payload.model_dump(exclude_unset=True)
        if data.get("email") is not None:
            if session.scalar(
                select(func.count(CustomUserCustomuser.id)).where(
                    CustomUserCustomuser.id != row.id,
                    func.lower(CustomUserCustomuser.email) == data["email"].lower(),
                )
            ):
                return DjangoStyleJSONResponse(
                    {"email": "This email is already in use."}, status_code=400
                )
        err = _validate_coords(data)
        if err is not None:
            return DjangoStyleJSONResponse(err, status_code=400)
        if (
            data.get("preferred_language") is not None
            and data["preferred_language"] not in _LANGUAGE_CHOICES
        ):
            return DjangoStyleJSONResponse(
                {"preferred_language": "Must be 'fr' or 'ar'."}, status_code=400
            )
        for k, v in data.items():
            setattr(row, k, v)
        session.flush()
        return _serialize_detail(row, _zones_count(session, row.id))


@router.delete("/users/{username}", summary="Admin: soft-delete (deactivate) one user")
def delete_user(username: str, user: AuthedUser = Depends(get_current_user)):
    guard = _admin_guard(user)
    if guard is not None:
        return guard
    with session_scope(commit=True) as session:
        row = _resolve_user(session, username)
        if row is None:
            return DjangoStyleJSONResponse(
                {"detail": "User not found."}, status_code=404
            )
        if row.id == user.id:
            return DjangoStyleJSONResponse(
                {"detail": "You cannot delete your own admin account."},
                status_code=400,
            )
        # Disabling an account also force-logs-out any live sessions.
        row.is_active = False
        row.sessions_revoked_at = _utcnow()
        session.flush()
        return DjangoStyleJSONResponse(None, status_code=204)


# ---------------------------------------------------------------------------
# /users/{username}/activate
# ---------------------------------------------------------------------------
@router.post("/users/{username}/activate", summary="Admin: toggle is_active")
def activate_user(
    username: str,
    payload: AdminActivateIn | None = None,
    user: AuthedUser = Depends(get_current_user),
):
    guard = _admin_guard(user)
    if guard is not None:
        return guard
    is_active = payload.is_active if payload is not None else None
    with session_scope(commit=True) as session:
        row = _resolve_user(session, username)
        if row is None:
            return DjangoStyleJSONResponse(
                {"detail": "User not found."}, status_code=404
            )
        if is_active is None:
            row.is_active = not row.is_active
        else:
            row.is_active = is_active
        if row.id == user.id and not row.is_active:
            return DjangoStyleJSONResponse(
                {"detail": "You cannot deactivate your own admin account."},
                status_code=400,
            )
        # Disabling the account immediately force-logs-out its live sessions.
        if not row.is_active:
            row.sessions_revoked_at = _utcnow()
        session.flush()
        return _serialize_detail(row, _zones_count(session, row.id))


# ---------------------------------------------------------------------------
# /users/{username}/password-reset
# ---------------------------------------------------------------------------
@router.post(
    "/users/{username}/password-reset", summary="Admin: reset a user's password"
)
def reset_password(
    username: str,
    payload: AdminResetPasswordIn | None = None,
    user: AuthedUser = Depends(get_current_user),
):
    guard = _admin_guard(user)
    if guard is not None:
        return guard
    with session_scope(commit=True) as session:
        row = _resolve_user(session, username)
        if row is None:
            return DjangoStyleJSONResponse(
                {"detail": "User not found."}, status_code=404
            )
        provided = payload.password if payload is not None else None
        new_password = provided or secrets.token_urlsafe(12)
        pw_errors = _validate_password_with_user(new_password, row.username, row.email)
        if pw_errors:
            return DjangoStyleJSONResponse({"password": pw_errors}, status_code=400)
        row.password = make_password(new_password)
        session.flush()
        log.info("admin %s reset password for %s", user.username, row.username)
        return {"username": row.username, "password": new_password}


# ---------------------------------------------------------------------------
# /users/{username}/sessions + /force-logout
# ---------------------------------------------------------------------------
@router.get(
    "/users/{username}/sessions",
    summary="Admin: a user's auth-token / session status",
)
def user_sessions(username: str, user: AuthedUser = Depends(get_current_user)):
    guard = _admin_guard(user)
    if guard is not None:
        return guard
    with session_scope() as session:
        row = _resolve_user(session, username)
        if row is None:
            return DjangoStyleJSONResponse(
                {"detail": "User not found."}, status_code=404
            )
        return _serialize_sessions(row)


@router.post(
    "/users/{username}/force-logout",
    summary="Admin: revoke all of a user's sessions (force logout)",
)
def force_logout_user(username: str, user: AuthedUser = Depends(get_current_user)):
    guard = _admin_guard(user)
    if guard is not None:
        return guard
    with session_scope(commit=True) as session:
        row = _resolve_user(session, username)
        if row is None:
            return DjangoStyleJSONResponse(
                {"detail": "User not found."}, status_code=404
            )
        row.sessions_revoked_at = _utcnow()
        session.flush()
        log.info("admin %s force-logged-out %s", user.username, row.username)
        return _serialize_sessions(row)
