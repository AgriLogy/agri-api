"""fastapp /admin/db — generic schema-driven CRUD over EVERY table (staff only).

Strangler port of ``agriapi/api/router_db.py`` (django-ninja). The Django
version introspects Django models (``django_apps.get_models()`` +
``model._meta``); this one introspects the agri.db SQLAlchemy metadata
(``AgriBase.registry.mappers`` + ``sqlalchemy.inspect``), so a new table in the
schema-of-record is manageable the moment it exists, with no per-table code.

Routes (mounted at the URL root; each carries its own ``/admin/db/*`` path):

  * GET    /admin/db/tables                 — list every model + row count
  * GET    /admin/db/tables/{key}/schema    — field schema for one model
  * GET    /admin/db/tables/{key}/rows      — paginated / searchable list
  * POST   /admin/db/tables/{key}/rows      — create a row
  * GET    /admin/db/tables/{key}/rows/{pk} — retrieve one row
  * PATCH  /admin/db/tables/{key}/rows/{pk} — update a row
  * DELETE /admin/db/tables/{key}/rows/{pk} — delete a row

``key`` is Django's ``app_label.modelname`` handle (e.g. ``analytics.zone``,
``CustomUser.customuser``). It is derived from the SQLAlchemy ``__tablename__``
so it stays byte-identical to what the Django endpoint emits — the agri-admin
"Database" page keeps working across the cutover.

PARITY NOTE — full byte-parity is *not* achievable for the introspection
responses (``/tables`` + ``/schema``): Django's ``verbose_name``, ``help_text``,
``choices``, per-field ``required``/``editable`` and the field *ordering* all
come from Django model metadata that simply does not exist in the DB schema /
SQLAlchemy layer. What IS byte-identical (and is what the frontend keys off):
the table ``key`` format, ``app_label``, ``model_name``, ``pk_field``, the set
of field names, each field's ``type``/``primary_key``/``nullable`` and the FK
``relation.to`` target. The row-CRUD JSON coercion (dates → ISO, Decimal →
float, bytes → None) and the ``{"detail": ...}`` error envelopes match too.
See ``tests/test_admindb_parity.py`` for the exact contract + the table-set
delta vs Django.
"""

from __future__ import annotations

import datetime
import decimal
import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    func,
    inspect,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError
from sqlalchemy.types import JSON

import agri.db  # noqa: F401 — import-for-side-effect: registers every model on AgriBase
from agri.core.database import session_scope
from agri.db.base import AgriBase
from fastapp.adminutil import record_audit
from fastapp.auth import AuthedUser, get_current_staff_user
from fastapp.json import DjangoStyleJSONResponse

router = APIRouter(tags=["admin-db"])

# Per-page caps for the row list (mirror the Django module constants).
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200

# --- table-key derivation --------------------------------------------------
# The frontend key is Django's ``model._meta.label_lower`` == f"{app_label}.
# {model_name}" (NB: only ``model_name`` is lowercased — ``app_label`` keeps its
# case, so the user table is "CustomUser.customuser"). Django's default db_table
# is f"{app_label}_{model_name}", so for those we recover (app_label, model_name)
# by splitting the tablename on the first "_" and stripping any remaining "_"
# from the model part. Two exceptions need help:
#
#   * ``assistant_conversation`` has a CUSTOM db_table whose split would give
#     "assistant.conversation", but the Django model is AssistantConversation →
#     "assistant.assistantconversation". Override it explicitly.
#   * The auto-created M2M through tables (``CustomUser_customuser_groups`` /
#     ``_user_permissions``) are NOT returned by Django's ``get_models()`` — hide
#     them so the fastapp set stays a clean subset of Django's.
_OVERRIDE_KEYS: dict[str, tuple[str, str]] = {
    "assistant_conversation": ("assistant", "assistantconversation"),
}
_HIDDEN_TABLES: set[str] = {
    "CustomUser_customuser_groups",
    "CustomUser_customuser_user_permissions",
}


def _key_parts(tablename: str) -> tuple[str, str, str]:
    """(app_label, model_name, key) for a tablename, matching Django's
    label_lower handle byte-for-byte."""
    if tablename in _OVERRIDE_KEYS:
        app_label, model_name = _OVERRIDE_KEYS[tablename]
    else:
        app_label, _, rest = tablename.partition("_")
        model_name = rest.replace("_", "").lower()
    return app_label, model_name, f"{app_label}.{model_name}"


def _build_registry() -> tuple[dict[str, Any], dict[str, str]]:
    """Map key → model class, and target-tablename → key (for FK resolution),
    over every registered agri.db model (minus the hidden M2M tables)."""
    by_key: dict[str, Any] = {}
    table_to_key: dict[str, str] = {}
    for mapper in AgriBase.registry.mappers:
        cls = mapper.class_
        tablename = cls.__tablename__
        if tablename in _HIDDEN_TABLES:
            continue
        _, _, key = _key_parts(tablename)
        by_key[key] = cls
        table_to_key[tablename] = key
    return by_key, table_to_key


_MODELS_BY_KEY, _TABLE_TO_KEY = _build_registry()


def _resolve_model(key: str):
    """Return the model class for ``app_label.modelname`` or ``None``."""
    return _MODELS_BY_KEY.get(key)


# ---------------------------------------------------------------------------
# Field / schema introspection
# ---------------------------------------------------------------------------


def _column_type(col) -> str:
    """Django ``get_internal_type`` → the router's coarse type string, derived
    from the SQLAlchemy column type. FK columns resolve to "fk" first."""
    if col.foreign_keys:
        return "fk"
    t = col.type
    # Order matters: Boolean is not an Integer; Text subclasses String; Float
    # subclasses Numeric; DateTime is not a Date.
    if isinstance(t, Boolean):
        return "boolean"
    if isinstance(t, Integer):
        return "integer"
    if isinstance(t, Float):
        return "float"
    if isinstance(t, Numeric):
        return "decimal"
    if isinstance(t, DateTime):
        return "datetime"
    if isinstance(t, Date):
        return "date"
    if isinstance(t, Time):
        return "time"
    if isinstance(t, (JSONB, JSON)):
        return "json"
    if isinstance(t, Text):
        return "text"
    if isinstance(t, String):
        return "string"
    return "string"


_TEXT_TYPES = (String, Text)


def _text_columns(model) -> list[Any]:
    """Columns Django would ``__icontains``-search: Char/Text/Slug/Email/URL —
    i.e. non-FK String/Text columns."""
    return [
        c
        for c in inspect(model).columns
        if isinstance(c.type, _TEXT_TYPES) and not c.foreign_keys
    ]


def _fk_target_key(col) -> str | None:
    """The frontend key of a FK column's referenced table (or None)."""
    for fk in col.foreign_keys:
        target_table = fk.column.table.name
        key = _TABLE_TO_KEY.get(target_table)
        if key is not None:
            return key
        # Referenced table has no agri.db model in scope (shouldn't happen for
        # the mirrored schema) — fall back to a derived key so the UI still has
        # a stable handle.
        _, _, derived = _key_parts(target_table)
        return derived
    return None


def _humanize(name: str) -> str:
    """A field/label default in the spirit of Django's ``name.replace('_', ' ')``
    (best-effort; Django's model verbose_name needs the CamelCase class name we
    don't have from the DB schema)."""
    if name == "id":
        return "ID"
    return name.replace("_", " ")


def _describe_column(col) -> dict[str, Any]:
    ctype = _column_type(col)
    is_fk = bool(col.foreign_keys)
    is_pk = bool(col.primary_key)
    has_default = col.default is not None or col.server_default is not None
    editable = not is_pk
    required = editable and not col.nullable and not has_default
    label_name = col.key[:-3] if (is_fk and col.key.endswith("_id")) else col.key
    info: dict[str, Any] = {
        "name": col.key,  # attname: FK → "<name>_id", scalar → "<name>"
        "label": _humanize(label_name),
        "type": ctype,
        "required": required,
        "editable": editable,
        "primary_key": is_pk,
        "nullable": bool(col.nullable),
        "help_text": "",
    }
    if is_fk:
        target = _fk_target_key(col)
        if target is not None:
            info["relation"] = {
                "to": target,
                "to_label": _humanize(target.split(".")[-1]),
            }
    max_len = getattr(col.type, "length", None)
    if max_len:
        info["max_length"] = max_len
    return info


def _pk_column(model):
    return inspect(model).primary_key[0]


def _schema(model, key: str) -> dict[str, Any]:
    app_label, model_name, _ = _key_parts(model.__tablename__)
    verbose_name = model_name  # best-effort (see PARITY NOTE)
    return {
        "key": key,
        "app_label": app_label,
        "model_name": model_name,
        "verbose_name": verbose_name,
        "verbose_name_plural": f"{verbose_name}s",
        "pk_field": _pk_column(model).key,
        "fields": [_describe_column(c) for c in inspect(model).columns],
    }


# ---------------------------------------------------------------------------
# Row serialization / coercion (mirrors Django ``_to_jsonable`` / ``_row_dict``)
# ---------------------------------------------------------------------------


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, memoryview)):
        return None
    return value


def _row_dict(obj, model) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in inspect(model).columns:
        out[col.key] = _to_jsonable(getattr(obj, col.key, None))
    out["__pk__"] = _to_jsonable(getattr(obj, _pk_column(model).key, None))
    out["__str__"] = str(obj)
    return out


def _writable_columns(model) -> dict[str, Any]:
    """attname → column for every editable (non-PK) column."""
    return {c.key: c for c in inspect(model).columns if not c.primary_key}


def _apply_values(obj, model, body: dict[str, Any]) -> None:
    writable = _writable_columns(model)
    for name, value in body.items():
        if name in writable:
            setattr(obj, name, value)


def _coerce_pk(model, pk: str) -> Any:
    """Best-effort cast of the path ``pk`` string to the PK column's type
    (integer PKs are the norm; anything else stays a string)."""
    if _column_type(_pk_column(model)) == "integer":
        try:
            return int(pk)
        except (TypeError, ValueError):
            return pk
    return pk


def _unknown_table(key: str) -> DjangoStyleJSONResponse:
    return DjangoStyleJSONResponse(
        {"detail": f"Unknown table '{key}'."}, status_code=404
    )


def _err_text(exc: Exception) -> str:
    # Surface the DB driver's message (Django surfaces IntegrityError/Validation
    # text the same way); keep it a single ``{"detail": ...}`` string.
    return str(getattr(exc, "orig", exc)).strip()


async def _parse_body(
    request: Request,
) -> tuple[dict | None, DjangoStyleJSONResponse | None]:
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return None, DjangoStyleJSONResponse(
            {"detail": "Invalid JSON body."}, status_code=400
        )
    if not isinstance(body, dict):
        return None, DjangoStyleJSONResponse(
            {"detail": "Body must be a JSON object."}, status_code=400
        )
    return body, None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/admin/db/tables", summary="Admin: list all tables")
def list_tables(user: AuthedUser = Depends(get_current_staff_user)):
    out = []
    with session_scope() as session:
        for key in sorted(_MODELS_BY_KEY):
            model = _MODELS_BY_KEY[key]
            app_label, model_name, _ = _key_parts(model.__tablename__)
            try:
                count = session.scalar(select(func.count()).select_from(model))
            except SQLAlchemyError:
                session.rollback()
                count = None
            out.append(
                {
                    "key": key,
                    "app_label": app_label,
                    "model_name": model_name,
                    "verbose_name": model_name,
                    "verbose_name_plural": f"{model_name}s",
                    "count": count,
                }
            )
    return out


@router.get("/admin/db/tables/{key}/schema", summary="Admin: table schema")
def table_schema(key: str, user: AuthedUser = Depends(get_current_staff_user)):
    model = _resolve_model(key)
    if model is None:
        return _unknown_table(key)
    return _schema(model, key)


@router.get("/admin/db/tables/{key}/rows", summary="Admin: list rows")
def list_rows(
    key: str,
    search: str = "",
    ordering: str = "",
    page: int = 1,
    page_size: int = _DEFAULT_PAGE_SIZE,
    user: AuthedUser = Depends(get_current_staff_user),
):
    model = _resolve_model(key)
    if model is None:
        return _unknown_table(key)

    columns = {c.key: c for c in inspect(model).columns}
    pk_col = _pk_column(model)

    stmt = select(model)
    count_stmt = select(func.count()).select_from(model)

    search = (search or "").strip()
    if search:
        text_cols = _text_columns(model)
        if text_cols:
            pattern = f"%{search}%"
            clause = or_(*(c.ilike(pattern) for c in text_cols))
            stmt = stmt.where(clause)
            count_stmt = count_stmt.where(clause)

    ordering = (ordering or "").strip()
    order_col = pk_col
    descending = False
    if ordering:
        field_name = ordering.lstrip("-")
        if field_name in columns:
            order_col = columns[field_name]
            descending = ordering.startswith("-")
    stmt = stmt.order_by(order_col.desc() if descending else order_col.asc())

    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(_MAX_PAGE_SIZE, max(1, int(page_size)))
    except (TypeError, ValueError):
        page_size = _DEFAULT_PAGE_SIZE

    with session_scope() as session:
        total = session.scalar(count_stmt)
        offset = (page - 1) * page_size
        rows = session.scalars(stmt.offset(offset).limit(page_size)).all()
        results = [_row_dict(obj, model) for obj in rows]
    return {
        "count": total,
        "page": page,
        "page_size": page_size,
        "results": results,
    }


@router.get("/admin/db/tables/{key}/rows/{pk}", summary="Admin: one row")
def retrieve_row(key: str, pk: str, user: AuthedUser = Depends(get_current_staff_user)):
    model = _resolve_model(key)
    if model is None:
        return _unknown_table(key)
    with session_scope() as session:
        obj = session.get(model, _coerce_pk(model, pk))
        if obj is None:
            return DjangoStyleJSONResponse(
                {"detail": "Row not found."}, status_code=404
            )
        return _row_dict(obj, model)


@router.post("/admin/db/tables/{key}/rows", summary="Admin: create row")
async def create_row(
    key: str, request: Request, user: AuthedUser = Depends(get_current_staff_user)
):
    model = _resolve_model(key)
    if model is None:
        return _unknown_table(key)
    body, err = await _parse_body(request)
    if err is not None:
        return err

    with session_scope(commit=True) as session:
        obj = model()
        _apply_values(obj, model, body)
        session.add(obj)
        try:
            session.flush()
        except (IntegrityError, DataError, ValueError, TypeError) as exc:
            session.rollback()
            return DjangoStyleJSONResponse({"detail": _err_text(exc)}, status_code=400)
        payload = _row_dict(obj, model)
        record_audit(
            session,
            user.id,
            f"db.{key}.create",
            key,
            getattr(obj, _pk_column(model).key, None),
        )
        return DjangoStyleJSONResponse(payload, status_code=201)


@router.patch("/admin/db/tables/{key}/rows/{pk}", summary="Admin: update row")
async def update_row(
    key: str,
    pk: str,
    request: Request,
    user: AuthedUser = Depends(get_current_staff_user),
):
    model = _resolve_model(key)
    if model is None:
        return _unknown_table(key)
    body, err = await _parse_body(request)
    if err is not None:
        return err

    with session_scope(commit=True) as session:
        obj = session.get(model, _coerce_pk(model, pk))
        if obj is None:
            return DjangoStyleJSONResponse(
                {"detail": "Row not found."}, status_code=404
            )
        _apply_values(obj, model, body)
        try:
            session.flush()
        except (IntegrityError, DataError, ValueError, TypeError) as exc:
            session.rollback()
            return DjangoStyleJSONResponse({"detail": _err_text(exc)}, status_code=400)
        payload = _row_dict(obj, model)
        record_audit(
            session,
            user.id,
            f"db.{key}.update",
            key,
            pk,
            {"keys": sorted(body.keys())},
        )
        return payload


@router.delete("/admin/db/tables/{key}/rows/{pk}", summary="Admin: delete row")
def delete_row(key: str, pk: str, user: AuthedUser = Depends(get_current_staff_user)):
    model = _resolve_model(key)
    if model is None:
        return _unknown_table(key)
    with session_scope(commit=True) as session:
        obj = session.get(model, _coerce_pk(model, pk))
        if obj is None:
            return DjangoStyleJSONResponse(
                {"detail": "Row not found."}, status_code=404
            )
        try:
            session.delete(obj)
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            return DjangoStyleJSONResponse({"detail": _err_text(exc)}, status_code=400)
        record_audit(session, user.id, f"db.{key}.delete", key, pk)
        return DjangoStyleJSONResponse(None, status_code=204)
