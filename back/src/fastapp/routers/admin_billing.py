"""fastapp /admin/billing — internal billing records (staff only).

Strangler port of ``apps/irrigation/router_billing.py`` (django-ninja).
Byte-parity with the Django version: same routes, same serialize shapes, same
404 envelopes, same ``payement_status`` mirroring onto the user. Reads/writes go
through the agri-core SQLAlchemy session (no Django ORM); ``created_at`` /
``issued_at`` are set in the app layer exactly like Django's ``auto_now_add``.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from agri.core.database import session_scope
from agri.db.billing import AnalyticsInvoice, AnalyticsPlan, AnalyticsSubscription
from agri.db.users import CustomUserCustomuser
from fastapp.adminutil import record_audit, username_for
from fastapp.auth import AuthedUser, get_current_staff_user
from fastapp.json import DjangoStyleJSONResponse

router = APIRouter(tags=["admin-billing"])


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# --- schemas ---------------------------------------------------------------
class PlanIn(BaseModel):
    name: str
    price_dh: float = 0
    interval: str = "monthly"
    features: list[str] = []
    is_active: bool = True


class SubscriptionIn(BaseModel):
    username: str
    plan_id: int
    period_start: str | None = None
    period_end: str | None = None


class InvoiceIn(BaseModel):
    subscription_id: int
    amount_dh: float = 0


# --- serializers -----------------------------------------------------------
def _plan(p: AnalyticsPlan) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "price_dh": p.price_dh,
        "interval": p.interval,
        "features": p.features or [],
        "is_active": p.is_active,
    }


def _subscription(session, s: AnalyticsSubscription) -> dict[str, Any]:
    plan_name = None
    if s.plan_id is not None:
        plan = session.get(AnalyticsPlan, s.plan_id)
        plan_name = plan.name if plan is not None else None
    return {
        "id": s.id,
        "username": username_for(session, s.user_id),
        "plan_id": s.plan_id,
        "plan_name": plan_name,
        "status": s.status,
        "period_start": s.period_start.isoformat() if s.period_start else None,
        "period_end": s.period_end.isoformat() if s.period_end else None,
    }


def _invoice(i: AnalyticsInvoice) -> dict[str, Any]:
    return {
        "id": i.id,
        "subscription_id": i.subscription_id,
        "amount_dh": i.amount_dh,
        "status": i.status,
        "issued_at": i.issued_at.isoformat() if i.issued_at else None,
        "paid_at": i.paid_at.isoformat() if i.paid_at else None,
    }


def _sync_payment_status(user: CustomUserCustomuser | None, sub_status: str) -> None:
    """Mirror the subscription status onto the user's payement_status."""
    new_status = "actif" if sub_status == "active" else "suspended"
    if user is not None and getattr(user, "payement_status", None) != new_status:
        user.payement_status = new_status


def _parse_date(value: str | None):
    return datetime.date.fromisoformat(value) if value else None


# --- plans -----------------------------------------------------------------
@router.get("/admin/billing/plans", summary="Admin: list plans")
def list_plans(user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope() as session:
        rows = session.scalars(select(AnalyticsPlan).order_by(AnalyticsPlan.id)).all()
        return [_plan(p) for p in rows]


@router.post("/admin/billing/plans", summary="Admin: create a plan")
def create_plan(payload: PlanIn, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope(commit=True) as session:
        p = AnalyticsPlan(
            name=payload.name,
            price_dh=payload.price_dh,
            interval=payload.interval,
            features=payload.features,
            is_active=payload.is_active,
            created_at=_utcnow(),
        )
        session.add(p)
        session.flush()
        record_audit(
            session, user.id, "plan.create", "plan", p.id, payload.model_dump()
        )
        return _plan(p)


@router.put("/admin/billing/plans/{plan_id}", summary="Admin: update a plan")
def update_plan(
    plan_id: int, payload: PlanIn, user: AuthedUser = Depends(get_current_staff_user)
):
    with session_scope(commit=True) as session:
        p = session.get(AnalyticsPlan, plan_id)
        if p is None:
            return DjangoStyleJSONResponse({"detail": "Not found"}, status_code=404)
        p.name = payload.name
        p.price_dh = payload.price_dh
        p.interval = payload.interval
        p.features = payload.features
        p.is_active = payload.is_active
        session.flush()
        record_audit(
            session, user.id, "plan.update", "plan", p.id, payload.model_dump()
        )
        return _plan(p)


@router.delete("/admin/billing/plans/{plan_id}", summary="Admin: delete a plan")
def delete_plan(plan_id: int, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope(commit=True) as session:
        p = session.get(AnalyticsPlan, plan_id)
        if p is not None:
            session.delete(p)
        record_audit(session, user.id, "plan.delete", "plan", plan_id)
        return {"status": "deleted"}


# --- subscriptions ---------------------------------------------------------
@router.get("/admin/billing/subscriptions", summary="Admin: list subscriptions")
def list_subscriptions(
    username: str | None = None, user: AuthedUser = Depends(get_current_staff_user)
):
    with session_scope() as session:
        criteria = []
        if username:
            uid = session.scalar(
                select(CustomUserCustomuser.id).where(
                    CustomUserCustomuser.username == username
                )
            )
            criteria.append(AnalyticsSubscription.user_id == (uid if uid else -1))
        rows = session.scalars(
            select(AnalyticsSubscription)
            .where(*criteria)
            .order_by(AnalyticsSubscription.id.desc())
        ).all()
        return [_subscription(session, s) for s in rows]


@router.post("/admin/billing/subscriptions", summary="Admin: assign a subscription")
def create_subscription(
    payload: SubscriptionIn, user: AuthedUser = Depends(get_current_staff_user)
):
    with session_scope(commit=True) as session:
        target = session.scalars(
            select(CustomUserCustomuser).where(
                CustomUserCustomuser.username == payload.username
            )
        ).first()
        if target is None:
            return DjangoStyleJSONResponse(
                {"detail": "User not found"}, status_code=404
            )
        if session.get(AnalyticsPlan, payload.plan_id) is None:
            return DjangoStyleJSONResponse(
                {"detail": "Plan not found"}, status_code=404
            )
        sub = AnalyticsSubscription(
            user_id=target.id,
            plan_id=payload.plan_id,
            status="active",
            period_start=_parse_date(payload.period_start),
            period_end=_parse_date(payload.period_end),
            created_at=_utcnow(),
        )
        session.add(sub)
        session.flush()
        _sync_payment_status(target, "active")
        record_audit(
            session,
            user.id,
            "subscription.create",
            "subscription",
            sub.id,
            payload.model_dump(),
        )
        return _subscription(session, sub)


@router.post(
    "/admin/billing/subscriptions/{sub_id}/cancel",
    summary="Admin: cancel a subscription",
)
def cancel_subscription(
    sub_id: int, user: AuthedUser = Depends(get_current_staff_user)
):
    with session_scope(commit=True) as session:
        sub = session.get(AnalyticsSubscription, sub_id)
        if sub is None:
            return DjangoStyleJSONResponse({"detail": "Not found"}, status_code=404)
        sub.status = "cancelled"
        target = (
            session.get(CustomUserCustomuser, sub.user_id)
            if sub.user_id is not None
            else None
        )
        _sync_payment_status(target, "cancelled")
        session.flush()
        record_audit(session, user.id, "subscription.cancel", "subscription", sub.id)
        return _subscription(session, sub)


# --- invoices --------------------------------------------------------------
@router.get("/admin/billing/invoices", summary="Admin: list invoices")
def list_invoices(
    username: str | None = None,
    subscription_id: int | None = None,
    user: AuthedUser = Depends(get_current_staff_user),
):
    with session_scope() as session:
        criteria = []
        if subscription_id:
            criteria.append(AnalyticsInvoice.subscription_id == subscription_id)
        if username:
            uid = session.scalar(
                select(CustomUserCustomuser.id).where(
                    CustomUserCustomuser.username == username
                )
            )
            sub_ids = session.scalars(
                select(AnalyticsSubscription.id).where(
                    AnalyticsSubscription.user_id == (uid if uid else -1)
                )
            ).all()
            criteria.append(AnalyticsInvoice.subscription_id.in_(sub_ids))
        rows = session.scalars(
            select(AnalyticsInvoice)
            .where(*criteria)
            .order_by(AnalyticsInvoice.id.desc())
        ).all()
        return [_invoice(i) for i in rows]


@router.post("/admin/billing/invoices", summary="Admin: create an invoice")
def create_invoice(
    payload: InvoiceIn, user: AuthedUser = Depends(get_current_staff_user)
):
    with session_scope(commit=True) as session:
        if session.get(AnalyticsSubscription, payload.subscription_id) is None:
            return DjangoStyleJSONResponse(
                {"detail": "Subscription not found"}, status_code=404
            )
        inv = AnalyticsInvoice(
            subscription_id=payload.subscription_id,
            amount_dh=payload.amount_dh,
            status="unpaid",
            issued_at=_utcnow(),
        )
        session.add(inv)
        session.flush()
        record_audit(
            session, user.id, "invoice.create", "invoice", inv.id, payload.model_dump()
        )
        return _invoice(inv)


@router.post(
    "/admin/billing/invoices/{invoice_id}/mark-paid",
    summary="Admin: mark invoice paid",
)
def mark_invoice_paid(
    invoice_id: int, user: AuthedUser = Depends(get_current_staff_user)
):
    with session_scope(commit=True) as session:
        inv = session.get(AnalyticsInvoice, invoice_id)
        if inv is None:
            return DjangoStyleJSONResponse({"detail": "Not found"}, status_code=404)
        inv.status = "paid"
        inv.paid_at = _utcnow()
        session.flush()
        record_audit(session, user.id, "invoice.mark_paid", "invoice", inv.id)
        return _invoice(inv)
