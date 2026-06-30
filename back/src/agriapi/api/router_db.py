"""Generic admin "database" router (django-ninja).

Mounted at ``/api/admin/db``. Gives staff users full CRUD over *every* model
in the project via ORM introspection — so a new table is manageable the moment
it exists, with no per-table code. Endpoints:

  * GET    /admin/db/tables                       — list every model + row count
  * GET    /admin/db/tables/{key}/schema          — field schema for one model
  * GET    /admin/db/tables/{key}/rows            — paginated/searchable list
  * POST   /admin/db/tables/{key}/rows            — create a row
  * GET    /admin/db/tables/{key}/rows/{pk}       — retrieve one row
  * PATCH  /admin/db/tables/{key}/rows/{pk}       — update a row
  * DELETE /admin/db/tables/{key}/rows/{pk}       — delete a row

``key`` is Django's ``app_label.modelname`` (lowercase, e.g. ``analytics.zone``).

All routes require JWT + ``is_staff`` (checked inline). Writes are audit-logged.
Django-internal bookkeeping tables are hidden by default.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import functools
import json
import logging
import operator
from typing import Any

from django.apps import apps as django_apps
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import IntegrityError, models
from django.db.models import Q
from ninja import Router
from ninja.responses import Response

from agriapi.api.auth import JwtAuth
from apps.irrigation.audit import record_audit

router = Router()
log = logging.getLogger(__name__)

# Bookkeeping tables that are noise for a business back-office. Everything else
# (including auth groups/permissions and all local apps) is exposed.
_HIDDEN_APP_LABELS = {"admin", "contenttypes", "sessions"}
_HIDDEN_MODELS = {"authtoken.tokenproxy"}

# Per-page caps for the row list.
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200


def _require_admin(request) -> Response | None:
    user = request.auth
    if user is None or not getattr(user, "is_staff", False):
        return Response({"detail": "Admin access required"}, status=403)
    return None


def _is_visible(model) -> bool:
    label = model._meta.label_lower
    if model._meta.app_label in _HIDDEN_APP_LABELS:
        return False
    if label in _HIDDEN_MODELS:
        return False
    return True


def _visible_models() -> list[Any]:
    return sorted(
        (m for m in django_apps.get_models() if _is_visible(m)),
        key=lambda m: m._meta.label_lower,
    )


def _resolve_model(key: str):
    """Return the model for ``app_label.modelname`` or ``None``."""
    try:
        app_label, model_name = key.split(".", 1)
    except ValueError:
        return None
    try:
        model = django_apps.get_model(app_label, model_name)
    except LookupError:
        return None
    if model is None or not _is_visible(model):
        return None
    return model


# ---------------------------------------------------------------------------
# Field introspection
# ---------------------------------------------------------------------------

_INTERNAL_TYPE_MAP = {
    "AutoField": "integer",
    "BigAutoField": "integer",
    "SmallAutoField": "integer",
    "IntegerField": "integer",
    "BigIntegerField": "integer",
    "SmallIntegerField": "integer",
    "PositiveIntegerField": "integer",
    "PositiveSmallIntegerField": "integer",
    "PositiveBigIntegerField": "integer",
    "FloatField": "float",
    "DecimalField": "decimal",
    "BooleanField": "boolean",
    "NullBooleanField": "boolean",
    "CharField": "string",
    "SlugField": "string",
    "EmailField": "string",
    "URLField": "string",
    "GenericIPAddressField": "string",
    "UUIDField": "string",
    "TextField": "text",
    "DateTimeField": "datetime",
    "DateField": "date",
    "TimeField": "time",
    "DurationField": "string",
    "JSONField": "json",
    "ForeignKey": "fk",
    "OneToOneField": "fk",
}


def _field_type(field) -> str:
    return _INTERNAL_TYPE_MAP.get(field.get_internal_type(), "string")


def _text_field_names(model) -> list[str]:
    out = []
    for f in model._meta.concrete_fields:
        if f.get_internal_type() in (
            "CharField",
            "TextField",
            "SlugField",
            "EmailField",
            "URLField",
        ):
            out.append(f.name)
    return out


def _describe_field(field) -> dict[str, Any]:
    ftype = _field_type(field)
    is_fk = field.is_relation and field.many_to_one
    column = field.attname  # FK -> "<name>_id", scalar -> "<name>"
    editable = bool(field.editable) and not field.primary_key
    has_default = field.has_default()
    required = (
        editable
        and not field.null
        and not field.blank
        and not has_default
        and not field.primary_key
    )
    info: dict[str, Any] = {
        "name": column,
        "label": str(getattr(field, "verbose_name", field.name)),
        "type": ftype,
        "required": required,
        "editable": editable,
        "primary_key": bool(field.primary_key),
        "nullable": bool(field.null),
        "help_text": str(getattr(field, "help_text", "") or ""),
    }
    if field.choices:
        info["choices"] = [
            {"value": v, "label": str(label_)} for v, label_ in field.choices
        ]
    if is_fk:
        rel = field.related_model
        info["relation"] = {
            "to": rel._meta.label_lower,
            "to_label": str(rel._meta.verbose_name),
        }
    max_len = getattr(field, "max_length", None)
    if max_len:
        info["max_length"] = max_len
    return info


def _schema(model) -> dict[str, Any]:
    fields = [_describe_field(f) for f in model._meta.concrete_fields]
    return {
        "key": model._meta.label_lower,
        "app_label": model._meta.app_label,
        "model_name": model._meta.model_name,
        "verbose_name": str(model._meta.verbose_name),
        "verbose_name_plural": str(model._meta.verbose_name_plural),
        "pk_field": model._meta.pk.attname,
        "fields": fields,
    }


# ---------------------------------------------------------------------------
# Row serialization / coercion
# ---------------------------------------------------------------------------


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, memoryview)):
        return None
    return value


def _row_dict(obj, model) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in model._meta.concrete_fields:
        out[f.attname] = _to_jsonable(getattr(obj, f.attname, None))
    # A stable primary-key handle for the UI, regardless of pk field name.
    out["__pk__"] = _to_jsonable(obj.pk)
    # A human label for FK pickers / row titles.
    out["__str__"] = str(obj)
    return out


def _writable_fields(model) -> dict[str, Any]:
    """Map of accepted write-key -> field (by attname), editable only."""
    out = {}
    for f in model._meta.concrete_fields:
        if f.editable and not f.primary_key:
            out[f.attname] = f
    return out


def _apply_values(obj, model, body: dict[str, Any]) -> None:
    writable = _writable_fields(model)
    for key, value in body.items():
        if key in writable:
            setattr(obj, key, value)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/admin/db/tables", auth=JwtAuth(), summary="Admin: list all tables")
def list_tables(request):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    out = []
    for model in _visible_models():
        try:
            count = model._default_manager.count()
        except Exception:  # pragma: no cover - unmanaged/odd tables
            count = None
        out.append(
            {
                "key": model._meta.label_lower,
                "app_label": model._meta.app_label,
                "model_name": model._meta.model_name,
                "verbose_name": str(model._meta.verbose_name),
                "verbose_name_plural": str(model._meta.verbose_name_plural),
                "count": count,
            }
        )
    return out


@router.get(
    "/admin/db/tables/{key}/schema", auth=JwtAuth(), summary="Admin: table schema"
)
def table_schema(request, key: str):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    model = _resolve_model(key)
    if model is None:
        return Response({"detail": f"Unknown table '{key}'."}, status=404)
    return _schema(model)


@router.get("/admin/db/tables/{key}/rows", auth=JwtAuth(), summary="Admin: list rows")
def list_rows(
    request,
    key: str,
    search: str = "",
    ordering: str = "",
    page: int = 1,
    page_size: int = _DEFAULT_PAGE_SIZE,
):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    model = _resolve_model(key)
    if model is None:
        return Response({"detail": f"Unknown table '{key}'."}, status=404)

    qs = model._default_manager.all()

    search = (search or "").strip()
    if search:
        text_fields = _text_field_names(model)
        if text_fields:
            q = functools.reduce(
                operator.or_,
                (Q(**{f"{name}__icontains": search}) for name in text_fields),
            )
            qs = qs.filter(q)

    ordering = (ordering or "").strip()
    if ordering:
        field_name = ordering.lstrip("-")
        try:
            model._meta.get_field(field_name)
            qs = qs.order_by(ordering)
        except FieldDoesNotExist:
            qs = qs.order_by(model._meta.pk.name)
    else:
        qs = qs.order_by(model._meta.pk.name)

    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(_MAX_PAGE_SIZE, max(1, int(page_size)))
    except (TypeError, ValueError):
        page_size = _DEFAULT_PAGE_SIZE

    total = qs.count()
    start = (page - 1) * page_size
    rows = [_row_dict(obj, model) for obj in qs[start : start + page_size]]
    return {
        "count": total,
        "page": page,
        "page_size": page_size,
        "results": rows,
    }


@router.get(
    "/admin/db/tables/{key}/rows/{pk}", auth=JwtAuth(), summary="Admin: one row"
)
def retrieve_row(request, key: str, pk: str):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    model = _resolve_model(key)
    if model is None:
        return Response({"detail": f"Unknown table '{key}'."}, status=404)
    obj = model._default_manager.filter(pk=pk).first()
    if obj is None:
        return Response({"detail": "Row not found."}, status=404)
    return _row_dict(obj, model)


def _parse_body(request) -> tuple[dict | None, Response | None]:
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return None, Response({"detail": "Invalid JSON body."}, status=400)
    if not isinstance(body, dict):
        return None, Response({"detail": "Body must be a JSON object."}, status=400)
    return body, None


@router.post("/admin/db/tables/{key}/rows", auth=JwtAuth(), summary="Admin: create row")
def create_row(request, key: str):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    model = _resolve_model(key)
    if model is None:
        return Response({"detail": f"Unknown table '{key}'."}, status=404)
    body, err = _parse_body(request)
    if err is not None:
        return err

    obj = model()
    _apply_values(obj, model, body)
    try:
        obj.save()
    except (IntegrityError, ValidationError, ValueError, TypeError) as exc:
        return Response({"detail": _err_text(exc)}, status=400)
    record_audit(
        request.auth, f"db.{model._meta.label_lower}.create", key, obj.pk, None
    )
    return Response(_row_dict(obj, model), status=201)


@router.patch(
    "/admin/db/tables/{key}/rows/{pk}", auth=JwtAuth(), summary="Admin: update row"
)
def update_row(request, key: str, pk: str):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    model = _resolve_model(key)
    if model is None:
        return Response({"detail": f"Unknown table '{key}'."}, status=404)
    obj = model._default_manager.filter(pk=pk).first()
    if obj is None:
        return Response({"detail": "Row not found."}, status=404)
    body, err = _parse_body(request)
    if err is not None:
        return err

    _apply_values(obj, model, body)
    try:
        obj.save()
    except (IntegrityError, ValidationError, ValueError, TypeError) as exc:
        return Response({"detail": _err_text(exc)}, status=400)
    record_audit(
        request.auth,
        f"db.{model._meta.label_lower}.update",
        key,
        obj.pk,
        {"keys": sorted(body.keys())},
    )
    return _row_dict(obj, model)


@router.delete(
    "/admin/db/tables/{key}/rows/{pk}", auth=JwtAuth(), summary="Admin: delete row"
)
def delete_row(request, key: str, pk: str):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    model = _resolve_model(key)
    if model is None:
        return Response({"detail": f"Unknown table '{key}'."}, status=404)
    obj = model._default_manager.filter(pk=pk).first()
    if obj is None:
        return Response({"detail": "Row not found."}, status=404)
    try:
        obj.delete()
    except (IntegrityError, models.ProtectedError) as exc:
        return Response({"detail": _err_text(exc)}, status=400)
    record_audit(request.auth, f"db.{model._meta.label_lower}.delete", key, pk, None)
    return Response(None, status=204)


def _err_text(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        try:
            return "; ".join(
                f"{k}: {', '.join(map(str, v))}" for k, v in exc.message_dict.items()
            )
        except Exception:
            return "; ".join(map(str, exc.messages))
    return str(exc)
