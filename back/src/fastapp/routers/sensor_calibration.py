"""fastapp /sensor-calibrations — per-sensor linear correction factors.

One row per ``(device_id, sensor_key)`` holding ``value * scale_a + offset_b``
plus a display ``unit`` and a free-text ``note``. This ticket only stores and
serves the factors — APPLYING them to readings is a later phase, so nothing
here touches the reading tables.

Storage: ``analytics_sensorcalibration`` (agri-db migration ``f4b6d2e8c1a9``),
reached with parameterised raw SQL because agri-api still pins an agri-core /
agri-db that predates the table.

The migration is not applied in production, so the read degrades to the
identity calibration (``scale_a=1``, ``offset_b=0``, ``configured=false``) and
the write is refused with a 400 naming the migration — see
``fastapp.schema_compat``.

General calibration (#460): instead of ``scale_a``/``offset_b`` a row may
carry an ``equation`` in ``x`` (the captured value) — e.g. ``a*x^2 + b*x + c``
— whose coefficients the SERVER fits from ``reference_points``
``[{captured, real}]`` (agri-core ``fit_equation``), storing the fitted
``coefficients`` and the ``fit_metrics`` (R², RMSE, max error …).
``POST /sensor-calibrations/fit`` runs the same fit statelessly so the editor
can preview it before saving. The equation columns are another agri-db
migration (``a7c3e9f1b2d4``); without them equation writes are refused with a
400 naming it and every read stays affine.

Ownership: a caller may only calibrate sensors on devices they own, resolved
through ``routers/selfreads._resolve_read_scope`` (technicians read the owner's
sensors within their granted zones and cannot write).
"""

from __future__ import annotations

import datetime
import json
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import JSON, bindparam, text

from agri.core.calibration_equation import (
    MAX_EQUATION_LENGTH,
    MAX_REFERENCE_POINTS,
    CalibrationFitError,
    EquationEvaluationError,
    EquationSyntaxError,
    FitResult,
    compile_equation,
    fit_equation,
    verify_equation,
)
from agri.core.database import session_scope
from agri.db.users import CustomUserCustomuser
from fastapp.auth import AuthedUser, get_current_user, require_level
from fastapp.json import DjangoStyleJSONResponse
from fastapp.routers.selfreads import _ReadScope, _resolve_read_scope
from fastapp.schema_compat import (
    SENSOR_CALIBRATION_EQUATION_UNAVAILABLE,
    SENSOR_CALIBRATION_UNAVAILABLE,
    sensor_calibration_available,
    sensor_calibration_equation_available,
)

router = APIRouter(tags=["sensor-calibration"])

_MAX_SENSOR_KEY = 64


class ReferencePointIn(BaseModel):
    """One reference measurement: the sensor CAPTURED this while the reference
    instrument / certified standard said the REAL value was that."""

    captured: float
    real: float


class CalibrationIn(BaseModel):
    scale_a: float = 1.0
    offset_b: float = 0.0
    unit: str = Field(default="", max_length=32)
    is_active: bool = True
    note: str | None = Field(default=None, max_length=2000)
    # #460 — general calibration. Blank/None = affine (scale_a, offset_b).
    equation: str | None = Field(default=None, max_length=MAX_EQUATION_LENGTH)
    # Coefficient values; with reference_points they only SEED the fit.
    coefficients: dict[str, float] | None = None
    reference_points: list[ReferencePointIn] | None = Field(
        default=None, max_length=MAX_REFERENCE_POINTS
    )


class FitIn(BaseModel):
    equation: str = Field(max_length=MAX_EQUATION_LENGTH)
    reference_points: list[ReferencePointIn] = Field(
        default_factory=list, max_length=MAX_REFERENCE_POINTS
    )
    # Starting values (non-linear equations) or, with no points, the values to
    # preview with.
    initial: dict[str, float] | None = None
    # Raw values to run through the fitted equation ("raw X → real Y").
    preview_raw: list[float] = Field(default_factory=list, max_length=50)


def _bad_request(detail: str) -> DjangoStyleJSONResponse:
    return DjangoStyleJSONResponse({"detail": detail}, status_code=400)


def _fit_payload(fit: FitResult) -> dict[str, Any]:
    return {
        "coefficients": fit.coefficients,
        "metrics": fit.metrics(),
        "points": [
            {
                "captured": p.captured,
                "real": p.real,
                "predicted": p.predicted,
                "residual": p.residual,
            }
            for p in fit.points
        ],
    }


class _Resolved(BaseModel):
    """What a PUT stores, once validated and (re)fitted."""

    equation: str = ""
    coefficients: dict[str, float] | None = None
    reference_points: list[dict[str, float]] | None = None
    fit_metrics: dict[str, Any] | None = None


def _resolve_equation(payload: CalibrationIn) -> _Resolved | str:
    """Validate the general-calibration part of a PUT. Returns the values to
    store, or an error message (→ 400). The SERVER fits: a client-sent
    coefficient set is only trusted when there are no points to fit it from."""
    points = [p.model_dump() for p in payload.reference_points or []]
    equation = (payload.equation or "").strip()
    try:
        if not equation:
            # Affine: points are verification only, scored against a·x + b.
            if not points:
                return _Resolved()
            fit = verify_equation(
                "a*x + b", {"a": payload.scale_a, "b": payload.offset_b}, points
            )
            return _Resolved(reference_points=points, fit_metrics=fit.metrics())
        compiled = compile_equation(equation)
        if points:
            fit = fit_equation(compiled, points, initial=payload.coefficients)
            return _Resolved(
                equation=compiled.source,
                coefficients=fit.coefficients,
                reference_points=points,
                fit_metrics=fit.metrics(),
            )
        given = payload.coefficients or {}
        missing = [n for n in compiled.coefficients if n not in given]
        if missing:
            return (
                f"Coefficient(s) {', '.join(missing)} need a value, or reference "
                "points to fit them from."
            )
        coefficients = {n: float(given[n]) for n in compiled.coefficients}
        return _Resolved(equation=compiled.source, coefficients=coefficients)
    except (EquationSyntaxError, CalibrationFitError, EquationEvaluationError) as exc:
        return str(exc)


def _scope(session, user_id: int) -> _ReadScope:
    row = session.get(CustomUserCustomuser, user_id)
    if row is None:  # pragma: no cover - a JWT for a deleted user
        return _ReadScope(owner_id=user_id, zone_ids=None)
    return _resolve_read_scope(session, row)


def _owned_device(session, device_id: int, scope: _ReadScope):
    """The device row when the caller may see it, else None (404 material).

    ``analytics_device`` always exists, so this check works even on a schema
    without the calibration table — ownership is never skipped by the shim.
    """
    row = session.execute(
        text(
            "SELECT id, zone_id FROM analytics_device "
            "WHERE id = :device AND user_id = :owner"
        ),
        {"device": device_id, "owner": scope.owner_id},
    ).first()
    if row is None or not scope.zone_allowed(row.zone_id):
        return None
    return row


def _default(
    device_id: int, sensor_key: str, available: bool, equation_available: bool = False
) -> dict[str, Any]:
    """The identity calibration: what an uncalibrated sensor reports, and what
    a deployment without the table reports for every sensor."""
    return {
        "device_id": device_id,
        "sensor_key": sensor_key,
        "scale_a": 1.0,
        "offset_b": 0.0,
        "unit": "",
        "is_active": False,
        "note": "",
        "equation": "",
        "coefficients": None,
        "reference_points": None,
        "fit_metrics": None,
        "configured": False,
        "schema_available": available,
        "equation_available": equation_available,
    }


def _json_column(value: Any) -> Any:
    """JSONB comes back decoded from Postgres, as text from sqlite."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _serialize(
    row, device_id: int, sensor_key: str, equation_available: bool
) -> dict[str, Any]:
    return {
        "device_id": device_id,
        "sensor_key": sensor_key,
        "scale_a": row.scale_a,
        "offset_b": row.offset_b,
        "unit": row.unit,
        "is_active": row.is_active,
        "note": row.note,
        "equation": getattr(row, "equation", "") or "",
        "coefficients": _json_column(getattr(row, "coefficients", None)),
        "reference_points": _json_column(getattr(row, "reference_points", None)),
        "fit_metrics": _json_column(getattr(row, "fit_metrics", None)),
        "configured": True,
        "schema_available": True,
        "equation_available": equation_available,
    }


_AFFINE_SELECT = "SELECT id, scale_a, offset_b, unit, is_active, note"
_EQUATION_SELECT = ", equation, coefficients, reference_points, fit_metrics"
_SELECT_WHERE = (
    " FROM analytics_sensorcalibration WHERE device_id = :device AND sensor_key = :key"
)


def _select_sql(equation_available: bool) -> str:
    extra = _EQUATION_SELECT if equation_available else ""
    return f"{_AFFINE_SELECT}{extra}{_SELECT_WHERE}"


# JSON bind params: SQLAlchemy serialises the dict/list for the dialect, and
# none_as_null stores SQL NULL (not the JSON literal 'null') for "no value".
_JSON_PARAMS = [
    bindparam(name, type_=JSON(none_as_null=True))
    for name in ("coefficients", "reference_points", "fit_metrics")
]


def _reject_key(sensor_key: str) -> DjangoStyleJSONResponse | None:
    if not sensor_key or len(sensor_key) > _MAX_SENSOR_KEY:
        return DjangoStyleJSONResponse(
            {"detail": "sensor_key must be 1-64 characters."}, status_code=400
        )
    return None


@router.get(
    "/sensor-calibrations/{device_id}/{sensor_key}",
    summary="Calibration for one (device, sensor_key)",
)
def get_calibration(
    device_id: int, sensor_key: str, user: AuthedUser = Depends(get_current_user)
):
    bad_key = _reject_key(sensor_key.strip())
    if bad_key is not None:
        return bad_key
    sensor_key = sensor_key.strip()
    with session_scope() as session:
        scope = _scope(session, user.id)
        if _owned_device(session, device_id, scope) is None:
            return DjangoStyleJSONResponse(
                {"detail": "Device not found."}, status_code=404
            )
        # COMPAT SHIM (#439) — without the table nothing is calibrated, which
        # is exactly the identity calibration. The client keeps working.
        if not sensor_calibration_available(session):
            return _default(device_id, sensor_key, available=False)
        equation_ok = sensor_calibration_equation_available(session)
        row = session.execute(
            text(_select_sql(equation_ok)), {"device": device_id, "key": sensor_key}
        ).first()
        if row is None:
            return _default(
                device_id, sensor_key, available=True, equation_available=equation_ok
            )
        return _serialize(row, device_id, sensor_key, equation_ok)


@router.post(
    "/sensor-calibrations/fit",
    summary="Fit a calibration equation to reference points (no storage)",
)
def fit_calibration(payload: FitIn, user: AuthedUser = Depends(get_current_user)):
    """Stateless: parse ``equation``, fit its coefficients to
    ``reference_points`` and run ``preview_raw`` through the result. With no
    coefficient in the equation the points only verify it; with no points the
    ``initial`` values are used as-is. Nothing is stored."""
    del user  # authenticated, but touches no farm data
    try:
        compiled = compile_equation(payload.equation)
        points = [p.model_dump() for p in payload.reference_points]
        if points:
            result = _fit_payload(
                fit_equation(compiled, points, initial=payload.initial)
            )
        else:
            given = payload.initial or {}
            missing = [n for n in compiled.coefficients if n not in given]
            if missing:
                return _bad_request(
                    f"Coefficient(s) {', '.join(missing)} need a value, or "
                    "reference points to fit them from."
                )
            result = {
                "coefficients": {n: float(given[n]) for n in compiled.coefficients},
                "metrics": None,
                "points": [],
            }
    except (EquationSyntaxError, CalibrationFitError, EquationEvaluationError) as exc:
        return _bad_request(str(exc))

    preview = []
    for raw in payload.preview_raw:
        try:
            preview.append(
                {"raw": raw, "real": compiled.evaluate(raw, result["coefficients"])}
            )
        except EquationEvaluationError as exc:
            preview.append({"raw": raw, "real": None, "error": str(exc)})
    return {
        "equation": compiled.source,
        "coefficient_names": list(compiled.coefficients),
        **result,
        "preview": preview,
    }


@router.put(
    "/sensor-calibrations/{device_id}/{sensor_key}",
    summary="Create or replace the calibration for one sensor",
)
def upsert_calibration(
    device_id: int,
    sensor_key: str,
    payload: CalibrationIn,
    user: AuthedUser = Depends(get_current_user),
    _: AuthedUser = Depends(require_level("editor")),
):
    bad_key = _reject_key(sensor_key.strip())
    if bad_key is not None:
        return bad_key
    sensor_key = sensor_key.strip()
    wants_equation = bool((payload.equation or "").strip())
    if payload.scale_a == 0 and not wants_equation:
        return DjangoStyleJSONResponse(
            {"detail": "scale_a must not be zero."}, status_code=400
        )
    resolved = _resolve_equation(payload)
    if isinstance(resolved, str):
        return _bad_request(resolved)
    with session_scope(commit=True) as session:
        scope = _scope(session, user.id)
        if scope.is_read_only:
            return DjangoStyleJSONResponse(
                {"detail": "Read-only (technician) access."}, status_code=403
            )
        if _owned_device(session, device_id, scope) is None:
            return DjangoStyleJSONResponse(
                {"detail": "Device not found."}, status_code=404
            )
        # COMPAT SHIM (#439) — refuse loudly rather than dropping a factor the
        # farmer believes was saved, mirroring the coordinate shim's writes.
        if not sensor_calibration_available(session):
            return DjangoStyleJSONResponse(
                {"detail": SENSOR_CALIBRATION_UNAVAILABLE}, status_code=400
            )
        equation_ok = sensor_calibration_equation_available(session)
        if not equation_ok and (wants_equation or payload.reference_points):
            # COMPAT SHIM (#460) — refuse rather than store a linear
            # calibration the technician did not ask for.
            return _bad_request(SENSOR_CALIBRATION_EQUATION_UNAVAILABLE)
        now = datetime.datetime.now(datetime.timezone.utc)
        params = {
            "device": device_id,
            "key": sensor_key,
            "scale_a": payload.scale_a,
            "offset_b": payload.offset_b,
            "unit": payload.unit,
            "is_active": payload.is_active,
            "note": payload.note or "",
            "now": now,
            **resolved.model_dump(),
        }
        # UNIQUE (device_id, sensor_key): update in place when it is already
        # there, so repeating the same PUT is idempotent (one row, ever).
        existing = session.execute(
            text(
                "SELECT id FROM analytics_sensorcalibration "
                "WHERE device_id = :device AND sensor_key = :key"
            ),
            {"device": device_id, "key": sensor_key},
        ).first()
        eq_cols = (
            ", equation, coefficients, reference_points, fit_metrics"
            if equation_ok
            else ""
        )
        eq_vals = (
            ", :equation, :coefficients, :reference_points, :fit_metrics"
            if equation_ok
            else ""
        )
        eq_set = (
            ", equation = :equation, coefficients = :coefficients, "
            "reference_points = :reference_points, fit_metrics = :fit_metrics"
            if equation_ok
            else ""
        )
        if existing is None:
            stmt = text(
                "INSERT INTO analytics_sensorcalibration "
                "(device_id, sensor_key, scale_a, offset_b, unit, is_active, "
                f" note, created_at, updated_at{eq_cols}) "
                "VALUES (:device, :key, :scale_a, :offset_b, :unit, :is_active, "
                f" :note, :now, :now{eq_vals})"
            )
            status = 201
        else:
            stmt = text(
                "UPDATE analytics_sensorcalibration SET scale_a = :scale_a, "
                "offset_b = :offset_b, unit = :unit, is_active = :is_active, "
                f"note = :note, updated_at = :now{eq_set} "
                "WHERE device_id = :device AND sensor_key = :key"
            )
            status = 200
        if equation_ok:
            stmt = stmt.bindparams(*_JSON_PARAMS)
        else:
            for name in ("equation", *(p.key for p in _JSON_PARAMS)):
                params.pop(name, None)
        session.execute(stmt, params)
        row = session.execute(
            text(_select_sql(equation_ok)), {"device": device_id, "key": sensor_key}
        ).first()
        return DjangoStyleJSONResponse(
            _serialize(row, device_id, sensor_key, equation_ok), status_code=status
        )
