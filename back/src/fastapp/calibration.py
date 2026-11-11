"""Per-sensor calibration application — the SINGLE read-path choke point (#446).

Calibration is stored one row per ``(device_id, sensor_key)`` in
``analytics_sensorcalibration`` (``scale_a``, ``offset_b``, ``unit``,
``is_active``); the maths lives in :mod:`agri.core.calibration`. The editor
(#439) only *stores* the factors — this module is what finally *applies* them
to readings.

**One point, on purpose.** #67's acceptance is "corrected values are used
consistently across dashboard, alerts and reports". The only way to guarantee
that is to correct a reading at ONE place per surface, all going through this
one helper:

* the read serializers (:func:`fastapp.sensors.hourly_readings` /
  :func:`fastapp.sensors.raw_readings`) — the dashboard/chart surface;
* :func:`fastapp.ingest.dispatch_alerts_for_reading` — the alert evaluator,
  which ALSO records the observed value into the alert-event history, so the
  reports surface is corrected by the very same call (never a second time).

Correcting in more than one place would let the surfaces disagree — that drift
IS the bug this closes.

Design:

* :func:`load_calibrations` — batch-load every needed ``(device_id, sensor_key)``
  calibration in ONE query and hand back a plain in-memory map. No per-reading
  query ever fires; callers map over the readings they already hold.
* :func:`corrected_value` — apply the affine correction (+ unit conversion when
  the calibration's unit differs from the reading's native unit) by delegating
  to agri-core. The maths is never reimplemented here.

Edge behaviour (all inherited from :mod:`agri.core.calibration`, made explicit):

* absent calibration → raw (identity);
* ``is_active = False`` → raw (the factor is disabled, not deleted);
* ``raw is None`` → ``None`` (a missing reading stays missing);
* a misconfigured unit (unknown / cross-dimension) → the affine correction
  alone, never a 500 on the dashboard nor a silently-dropped alert;
* an equation calibration (#460) that cannot be evaluated at this raw value
  (outside its domain: ``log`` of a negative, division by zero, …) → ``None``.
  The reading is reported missing rather than as an uncorrected number that
  would look calibrated, and the alert path skips it.

The store predates the pinned agri-db in production, so — like every other
consumer of this table — the batch load asks :mod:`fastapp.schema_compat`
first and treats an absent table as "nothing is calibrated" (identity).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy import bindparam, text

from agri.core.calibration import (
    Calibration,
    CalibrationError,
    apply_calibration,
    calibrated_value,
)
from fastapp.schema_compat import (
    SENSOR_CALIBRATION_TABLE,
    sensor_calibration_available,
    sensor_calibration_equation_available,
)

log = logging.getLogger(__name__)

# The map key: a device id + the sensor_key its stream is calibrated under.
CalibrationKey = tuple[int, str]

# Two plain ``IN`` lists (not a row-value ``(device_id, sensor_key) IN (...)``):
# the cross-product is filtered back to the wanted pairs in Python, which keeps
# the SQL portable across dialects (the sqlite ingest mirror renders row-value
# IN differently from Postgres). A request keys one sensor_key over a handful of
# devices, so the over-fetch is negligible.
_AFFINE_COLUMNS = "device_id, sensor_key, scale_a, offset_b, unit, is_active"
# #460 — only selected when agri-db a7c3e9f1b2d4 is applied (schema shim).
_EQUATION_COLUMNS = ", equation, coefficients"
_LOAD_WHERE = (
    f" FROM {SENSOR_CALIBRATION_TABLE} "
    "WHERE device_id IN :devices AND sensor_key IN :keys"
)


def load_calibrations(
    session: Any, pairs: Iterable[tuple[int | None, str | None]]
) -> dict[CalibrationKey, Calibration]:
    """Active calibrations for a set of ``(device_id, sensor_key)`` pairs, in ONE
    query, as an in-memory ``{(device_id, sensor_key): Calibration}`` map.

    Pairs with a null ``device_id`` or blank ``sensor_key`` are dropped (a
    reading with no device can carry no per-device calibration). A pair with no
    stored row is simply absent from the map, which the caller reads as identity
    — so an un-calibrated sensor costs nothing.

    Returns an empty map when the calibration table is not on this deployment
    (schema shim), which is exactly "nothing is calibrated".
    """
    wanted: set[CalibrationKey] = {
        (int(device_id), str(sensor_key))
        for device_id, sensor_key in pairs
        if device_id is not None and sensor_key
    }
    if not wanted:
        return {}
    if not sensor_calibration_available(session):
        return {}
    columns = _AFFINE_COLUMNS
    if sensor_calibration_equation_available(session):
        columns += _EQUATION_COLUMNS
    stmt = text(f"SELECT {columns}{_LOAD_WHERE}").bindparams(
        bindparam("devices", expanding=True),
        bindparam("keys", expanding=True),
    )
    rows = session.execute(
        stmt,
        {
            "devices": sorted({device_id for device_id, _ in wanted}),
            "keys": sorted({sensor_key for _, sensor_key in wanted}),
        },
    ).all()
    loaded: dict[CalibrationKey, Calibration] = {}
    for row in rows:
        key = (row.device_id, row.sensor_key)
        if key not in wanted:
            continue
        try:
            loaded[key] = Calibration.from_row(row)
        except CalibrationError:
            # Corrupt stored coefficients must not 500 every chart of the
            # farm; the stream reads uncorrected and the operator is told.
            log.warning("unreadable calibration for %s; ignoring it", key)
    return loaded


def corrected_value(
    raw: float | None,
    calibration: Calibration | None,
    *,
    sensor_key: str | None = None,
    native_unit: str | None = None,
) -> float | None:
    """The reading a client should see: raw corrected by ``calibration``.

    Delegates the arithmetic to :func:`agri.core.calibration.calibrated_value`
    (affine correction, then unit conversion only when the calibration's unit
    differs from ``native_unit``). ``None`` / inactive / absent all fall through
    to the raw value.

    A unit the agri-core table cannot convert (unknown, or spanning two physical
    dimensions — e.g. a calibration mistakenly stored in ``°C`` for a ``pH``
    sensor) would make agri-core raise. On a read endpoint that would 500 the
    whole chart, and on the alert path it would drop the firing — so here it
    degrades to the affine correction alone. The same fallback fires on both
    surfaces, so they still agree.
    """
    if raw is None:
        return None
    if calibration is None or not calibration.is_active:
        return raw
    try:
        return calibrated_value(
            raw,
            calibration,
            sensor_key=sensor_key,
            target_unit=native_unit or None,
        )
    except CalibrationError:
        try:
            return apply_calibration(raw, calibration)
        except CalibrationError:
            # Equation outside its domain at this raw value: missing, never
            # a fabricated or silently-uncorrected number.
            return None


__all__ = [
    "CalibrationKey",
    "load_calibrations",
    "corrected_value",
]
