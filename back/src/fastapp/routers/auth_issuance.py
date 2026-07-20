"""fastapp auth issuance (F9) — the login/token surface.

Strangler port of ``apps/users/router.py`` (django-ninja ``/auth`` router) plus
the two remaining legacy DRF endpoints in ``apps/users/urls.py``:

  * ``POST /auth/signup``            public signup (+ per-user bootstrap rows)
  * ``POST /auth/sessions``          public sign-in → JWT pair
  * ``POST /auth/admin-sessions``    sign-in, response omits ``is_staff``
  * ``DELETE /auth/sessions``        log out everywhere (bump revoked-at)
  * ``POST /auth/token/``            simplejwt TokenObtainPairView
  * ``POST /auth/token/refresh/``    revocation-aware TokenRefreshView

Tokens are minted by :mod:`fastapp.tokens` (simplejwt-compatible claims) and
verified by :mod:`fastapp.auth`; passwords by :mod:`fastapp.passwords`
(pbkdf2_sha256, Django ``check_password`` compatible). No Django ORM — all DB
access is via the agri-core SQLAlchemy session. The Django ``login()`` session
cookie is intentionally NOT set: the frontends are Bearer + SSO-localStorage
only (no ``sessionid`` / ``credentials: include`` anywhere in agri-front /
agri-web).
"""

from __future__ import annotations

import datetime
import logging
import time

import jwt
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select, text
from starlette.responses import JSONResponse as _CompactJSONResponse

from agri.core.database import session_scope
from agri.db.analytics import (
    AnalyticsGraphname,
    AnalyticsSensorcolor,
)
from agri.db.users import CustomUserCustomuser
from fastapp.auth import AuthedUser, get_current_user, token_session_revoked
from fastapp.json import DjangoStyleJSONResponse
from fastapp.passwords import make_password, validate_password, verify_password
from fastapp.settings import get_settings
from fastapp.tokens import mint_access, mint_tokens

router = APIRouter(tags=["auth"])
log = logging.getLogger(__name__)

# Best-effort in-process login lockout — Django uses ``cache`` (LocMemCache,
# no CACHES override in settings), which is per-process too, so this matches
# the live semantics: 5 failures locks a username out for 5 minutes.
_LOGIN_LOCKOUT_LIMIT = 5
_LOGIN_LOCKOUT_TIMEOUT_S = 300
_login_attempts: dict[str, tuple[int, float]] = {}


def _lockout_reset() -> None:  # test hook
    _login_attempts.clear()


def _lockout_get(username: str) -> int:
    entry = _login_attempts.get(username)
    if entry is None:
        return 0
    count, expiry = entry
    if time.monotonic() >= expiry:
        _login_attempts.pop(username, None)
        return 0
    return count


def _lockout_bump(username: str, attempts: int) -> None:
    _login_attempts[username] = (
        attempts + 1,
        time.monotonic() + _LOGIN_LOCKOUT_TIMEOUT_S,
    )


def _lockout_clear(username: str) -> None:
    _login_attempts.pop(username, None)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# GraphName / SensorColor field defaults (Django model defaults — no DB
# defaults), applied on signup by the post_save(User) bootstrap signal.
_GRAPHNAME_DEFAULTS = {
    "soil_irrigation": "Irrigation du sol",
    "soil_ph": "pH du sol",
    "soil_conductivity": "Conductivité du sol",
    "soil_moisture": "Humidité du sol",
    "soil_temperature": "Température du sol",
    "et0": "Taux d'évapotranspiration",
    "precipitation_rate": "Taux de précipitation",
    "wind_speed": "Vitesse du vent",
    "solar_radiation": "Rayonnement solaire",
    "pressure_weather": "Pression atmosphérique",
    "wind_direction": "Direction du vent",
    "humidity_weather": "Humidité de l'air",
    "temperature_weather": "Température de l'air",
    "temperature_humidity_weather": "Température et humidité de l'air",
    "precipitation_humidity_rate": "Taux de précipitation et humidité",
    "pluviometrie": "Cumule de pluie tombée",
    "data_table": "Tableau de données",
}
_SENSORCOLOR_DEFAULTS = {
    "precipitation_rate_color": "#3D8D7A",
    "humidity_weather_color": "#2A6F97",
    "wind_speed_color": "#FFB703",
    "solar_radiation_color": "#E63946",
    "pressure_weather_color": "#F4A261",
    "wind_direction_color": "#6A0572",
    "temperature_weather_color": "#E76F51",
    "et0_color": "#497D74",
    "ec_soil_medium_color": "#2A9D8F",
    "soil_temperature_medium_color": "#264653",
    "soil_ec_high_color": "#8A2BE2",
    "ec_soil_low_color": "#D4A373",
    "soil_moisture_medium_color": "#6D597A",
    "soil_moisture_high_color": "#C8553D",
    "soil_moisture_low_color": "#457B9D",
    "ph_soil_color": "#023E8A",
    "soil_temperature_low_color": "#8D99AE",
    "soil_temperature_high_color": "#E9C46A",
}


def _record_login(request: Request, username: str, user_id: int | None, success: bool):
    """Best-effort sign-in event for the monitoring back-office. Never raises —
    ``analytics_loginevent`` is unmanaged and must not break authentication."""
    try:
        xff = request.headers.get("x-forwarded-for", "")
        ip = (xff.split(",")[0].strip() if xff else (request.client.host or "")) or ""
        ua = request.headers.get("user-agent", "") or ""
        with session_scope(commit=True) as session:
            session.execute(
                text(
                    "INSERT INTO analytics_loginevent "
                    "(username, user_id, success, ip, user_agent, created_at) "
                    "VALUES (:u, :uid, :s, :ip, :ua, :ts)"
                ),
                {
                    "u": (username or "")[:150],
                    "uid": user_id,
                    "s": success,
                    "ip": ip[:64],
                    "ua": ua[:255],
                    "ts": _utcnow(),
                },
            )
    except Exception:  # pragma: no cover - fail-soft
        pass


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class SignUpIn(BaseModel):
    username: str
    email: str
    firstname: str
    lastname: str
    phone_number: str
    password: str


class SignInIn(BaseModel):
    username: str
    password: str


class TokenObtainIn(BaseModel):
    username: str
    password: str


class TokenRefreshIn(BaseModel):
    refresh: str


def _authenticate(session, username: str, password: str) -> CustomUserCustomuser | None:
    """Django ``authenticate()`` + ModelBackend: username lookup, password
    check, and ``is_active`` gate (inactive users never authenticate).

    The username match is case-insensitive and whitespace-trimmed: admins create
    users with mixed case (e.g. "Ahmed"), but people sign in however they type it
    ("ahmed", " ahmed"). Uniqueness is already enforced case-insensitively at
    creation (``func.lower(username)`` in users.py / signup), so at most one row
    matches."""
    user = session.scalars(
        select(CustomUserCustomuser).where(
            func.lower(CustomUserCustomuser.username) == username.strip().lower()
        )
    ).first()
    if user is None or not user.password:
        return None
    if not verify_password(password, user.password):
        return None
    if not user.is_active:
        return None
    return user


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------
@router.post("/auth/signup", status_code=201, summary="Public user signup")
def signup(payload: SignUpIn):
    with session_scope(commit=True) as session:
        if session.scalar(
            select(func.count())
            .select_from(CustomUserCustomuser)
            .where(CustomUserCustomuser.email == payload.email)
        ):
            return DjangoStyleJSONResponse(
                {"email": "This email is already in use."}, status_code=400
            )
        if session.scalar(
            select(func.count())
            .select_from(CustomUserCustomuser)
            # Case-insensitive, matching the admin-create check (users.py) and the
            # case-insensitive login lookup — never allow "Bob" alongside "bob".
            .where(
                func.lower(CustomUserCustomuser.username)
                == payload.username.strip().lower()
            )
        ):
            return DjangoStyleJSONResponse(
                {"username": "This username is already in use."}, status_code=400
            )
        pw_errors = validate_password(payload.password)
        if pw_errors:
            return DjangoStyleJSONResponse({"password": pw_errors}, status_code=400)

        user = CustomUserCustomuser(
            username=payload.username,
            email=payload.email,
            firstname=payload.firstname,
            lastname=payload.lastname,
            phone_number=payload.phone_number,
            password=make_password(payload.password),
            is_superuser=False,
            is_staff=False,
            is_active=True,
            is_technician=False,
            payement_status="actif",
            preferred_language="fr",
            notify_every=240,
            date_joined=_utcnow(),
        )
        session.add(user)
        session.flush()  # assign user.id
        # post_save(User) bootstrap: per-user GraphName + SensorColor rows.
        session.add(
            AnalyticsGraphname(user_id=user.id, zone_id=None, **_GRAPHNAME_DEFAULTS)
        )
        session.add(
            AnalyticsSensorcolor(user_id=user.id, zone_id=None, **_SENSORCOLOR_DEFAULTS)
        )
    return DjangoStyleJSONResponse(
        {"status": "Account created successfully"}, status_code=201
    )


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------
def _signin_core(request: Request, payload: SignInIn):
    if not payload.username or not payload.password:
        return DjangoStyleJSONResponse(
            {"error": "Username and password are required."}, status_code=400
        )
    attempts = _lockout_get(payload.username)
    if attempts >= _LOGIN_LOCKOUT_LIMIT:
        return DjangoStyleJSONResponse(
            {"error": "Too many login attempts. Please try again later."},
            status_code=429,
        )
    with session_scope() as session:
        user = _authenticate(session, payload.username, payload.password)
        if user is None:
            _lockout_bump(payload.username, attempts)
            _record_login(request, payload.username, None, success=False)
            return DjangoStyleJSONResponse(
                {"error": "Invalid credentials"}, status_code=401
            )
        uid = user.id
        is_staff = user.is_staff
        is_technician = bool(getattr(user, "is_technician", False))
    _lockout_clear(payload.username)
    _record_login(request, payload.username, uid, success=True)
    refresh, access = mint_tokens(uid)
    return {
        "refresh": refresh,
        "access": access,
        "is_staff": is_staff,
        "is_technician": is_technician,
    }


@router.post("/auth/sessions", summary="Public user sign-in (issue JWT)")
def signin(request: Request, payload: SignInIn):
    return _signin_core(request, payload)


@router.post(
    "/auth/admin-sessions", summary="Admin-only sign-in (response omits is_staff)"
)
def admin_signin(request: Request, payload: SignInIn):
    result = _signin_core(request, payload)
    if isinstance(result, dict):
        result.pop("is_staff", None)
    return result


@router.delete("/auth/sessions", summary="Log out everywhere (revoke sessions)")
def logout_everywhere(user: AuthedUser = Depends(get_current_user)):
    revoked_at = _utcnow()
    with session_scope(commit=True) as session:
        row = session.get(CustomUserCustomuser, user.id)
        if row is not None:
            row.sessions_revoked_at = revoked_at
    log.info("user %s logged out everywhere (sessions revoked)", user.username)
    return {"success": True, "sessions_revoked_at": revoked_at.isoformat()}


# ---------------------------------------------------------------------------
# Legacy DRF token endpoints
#
# These were DRF views, whose JSONRenderer emits COMPACT json (separators
# ``(",", ":")``, ``ensure_ascii`` off) — unlike the django-ninja endpoints
# above, whose bodies are spaced (DjangoStyleJSONResponse). Starlette's stock
# JSONResponse is byte-compatible with DRF's renderer, so use it here.
# ---------------------------------------------------------------------------
@router.post("/auth/token/", summary="Obtain a JWT pair (simplejwt)")
def token_obtain(payload: TokenObtainIn):
    with session_scope() as session:
        user = _authenticate(session, payload.username, payload.password)
        if user is None:
            return _CompactJSONResponse(
                {"detail": "No active account found with the given credentials"},
                status_code=401,
            )
        uid = user.id
    refresh, access = mint_tokens(uid)
    return _CompactJSONResponse({"refresh": refresh, "access": access})


def _invalid_token_response():
    return _CompactJSONResponse(
        {"detail": "Token is invalid or expired", "code": "token_not_valid"},
        status_code=401,
    )


@router.post(
    "/auth/token/refresh/", summary="Refresh an access token (revocation-aware)"
)
def token_refresh(payload: TokenRefreshIn):
    try:
        claims = jwt.decode(
            payload.refresh,
            get_settings().secret_key,
            algorithms=["HS256"],
            options={"require": ["exp", "iat"]},
        )
    except jwt.PyJWTError:
        return _invalid_token_response()
    if claims.get("token_type") != "refresh":
        return _invalid_token_response()
    user_id = claims.get("user_id")
    if user_id is None:
        return _invalid_token_response()
    # Revocation-aware refresh (RevocationAwareTokenRefreshView): a disabled
    # account or a session revoked after the token's iat can't mint a new one.
    with session_scope() as session:
        user = session.get(CustomUserCustomuser, user_id)
        if user is not None:
            if not user.is_active:
                return _CompactJSONResponse(
                    {"detail": "Account is disabled."}, status_code=401
                )
            if token_session_revoked(user.sessions_revoked_at, claims.get("iat")):
                return _CompactJSONResponse(
                    {"detail": "Session has been revoked."}, status_code=401
                )
    return _CompactJSONResponse({"access": mint_access(user_id)})
