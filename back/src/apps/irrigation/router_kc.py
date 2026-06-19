"""Crop-calendar (Kc) CRUD endpoints (django-ninja).

A *Kc* is a crop coefficient definition the farmer attaches to a zone: a crop
(name + plant) split into growth-stage *periods*, each with a date range and a
Kc value. These feed ETc / irrigation advice (ETc = ET0 × Kc).

The ``analytics_kc`` / ``analytics_kcperiod`` / ``analytics_kcperiodassignment``
tables already exist in the agri-db schema (managed models, migration 0028), so
no self-deploy is needed. Writes are direct + caller-scoped (no affirmation
gate — the affirmation workflow only records requests, it does not apply them).
"""

from __future__ import annotations

from datetime import date

from django.db import transaction
from ninja import Router, Schema
from ninja.responses import Response

from agriapi.api.auth import JwtAuth
from agriapi.api.scope import block_if_technician
from analytics.models import Kc, KcPeriod, KcPeriodAssignment, Zone

router = Router()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class KcPeriodIn(Schema):
    period_name: str
    start_date: date
    end_date: date
    kc_value: float


class KcWriteIn(Schema):
    """Create/replace body. ``periods`` fully replaces the crop's stages."""

    name: str
    plant_name: str
    zone_id: int | None = None
    periods: list[KcPeriodIn] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize(kc: Kc) -> dict:
    periods = [
        {
            "id": a.period.id,
            "period_name": a.period.period_name,
            "start_date": a.period.start_date.isoformat(),
            "end_date": a.period.end_date.isoformat(),
            "kc_value": a.period.kc_value,
        }
        for a in kc.periods.select_related("period").all()
    ]
    periods.sort(key=lambda p: p["start_date"])
    return {
        "id": kc.id,
        "name": kc.name,
        "plant_name": kc.plant_name,
        "zone_id": kc.zone_id,
        "zone_name": kc.zone.name if kc.zone_id else None,
        "number_of_periods": kc.number_of_periods,
        "periods": periods,
    }


def _replace_periods(kc: Kc, periods: list[KcPeriodIn]) -> None:
    """Drop the crop's existing stages and recreate them from ``periods``."""
    old_period_ids = list(
        kc.periods.values_list("period_id", flat=True)  # type: ignore[attr-defined]
    )
    kc.periods.all().delete()  # type: ignore[attr-defined]
    KcPeriod.objects.filter(id__in=old_period_ids).delete()
    for p in periods:
        period = KcPeriod.objects.create(
            period_name=p.period_name,
            start_date=p.start_date,
            end_date=p.end_date,
            kc_value=p.kc_value,
        )
        KcPeriodAssignment.objects.create(kc=kc, period=period)
    kc.number_of_periods = len(periods)
    kc.save(update_fields=["number_of_periods"])


def _owned(request, kc_id: int) -> Kc | None:
    return Kc.objects.filter(id=kc_id, user=request.auth).first()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", auth=JwtAuth(), summary="List the caller's crop calendars (Kc)")
def list_kc(request, zone_id: int | None = None):
    blocked = block_if_technician(request)
    if blocked:
        return blocked
    qs = Kc.objects.filter(user=request.auth).select_related("zone").order_by("id")
    if zone_id is not None:
        qs = qs.filter(zone_id=zone_id)
    return [_serialize(kc) for kc in qs]


@router.post("", auth=JwtAuth(), summary="Create a crop calendar (Kc)")
def create_kc(request, payload: KcWriteIn):
    blocked = block_if_technician(request)
    if blocked:
        return blocked
    zone = None
    if payload.zone_id is not None:
        zone = Zone.objects.filter(id=payload.zone_id, user=request.auth).first()
        if zone is None:
            return Response({"detail": "Zone not found."}, status=404)
    with transaction.atomic():
        kc = Kc.objects.create(
            user=request.auth,
            zone=zone,
            name=payload.name,
            plant_name=payload.plant_name,
            number_of_periods=0,
        )
        _replace_periods(kc, payload.periods)
    return Response(_serialize(kc), status=201)


@router.get("/{kc_id}", auth=JwtAuth(), summary="Fetch one crop calendar")
def get_kc(request, kc_id: int):
    kc = _owned(request, kc_id)
    if kc is None:
        return Response({"detail": "Kc not found."}, status=404)
    return _serialize(kc)


@router.put("/{kc_id}", auth=JwtAuth(), summary="Replace one crop calendar")
def update_kc(request, kc_id: int, payload: KcWriteIn):
    kc = _owned(request, kc_id)
    if kc is None:
        return Response({"detail": "Kc not found."}, status=404)
    zone = kc.zone
    if payload.zone_id is not None:
        zone = Zone.objects.filter(id=payload.zone_id, user=request.auth).first()
        if zone is None:
            return Response({"detail": "Zone not found."}, status=404)
    with transaction.atomic():
        kc.name = payload.name
        kc.plant_name = payload.plant_name
        kc.zone = zone
        kc.save(update_fields=["name", "plant_name", "zone"])
        _replace_periods(kc, payload.periods)
    return _serialize(kc)


@router.delete("/{kc_id}", auth=JwtAuth(), summary="Delete a crop calendar")
def delete_kc(request, kc_id: int):
    kc = _owned(request, kc_id)
    if kc is None:
        return Response({"detail": "Kc not found."}, status=404)
    period_ids = list(kc.periods.values_list("period_id", flat=True))  # type: ignore[attr-defined]
    kc.delete()
    KcPeriod.objects.filter(id__in=period_ids).delete()
    return Response({"status": "deleted"}, status=200)
