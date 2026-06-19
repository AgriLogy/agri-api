"""Admin billing router (django-ninja) — mounted at ``/admin/billing``.

Internal records only (no payment gateway). Endpoints (JWT + ``is_staff``):

  * GET, POST                 /admin/billing/plans
  * PUT, DELETE               /admin/billing/plans/<plan_id>
  * GET, POST                 /admin/billing/subscriptions
  * POST                      /admin/billing/subscriptions/<sub_id>/cancel
  * GET, POST                 /admin/billing/invoices
  * POST                      /admin/billing/invoices/<invoice_id>/mark-paid

Subscription status is mirrored onto ``CustomUser.payement_status``
(active → "actif", otherwise → "suspended").
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.utils import timezone
from ninja import Router, Schema
from ninja.responses import Response

from agriapi.api.auth import JwtAuth
from apps.irrigation.audit import record_audit
from apps.irrigation.models import Invoice, Plan, Subscription
from apps.irrigation.router_admin import _require_admin

router = Router()
User = get_user_model()


# --- schemas ---------------------------------------------------------------
class PlanIn(Schema):
    name: str
    price_dh: float = 0
    interval: str = "monthly"
    features: list[str] = []
    is_active: bool = True


class SubscriptionIn(Schema):
    username: str
    plan_id: int
    period_start: str | None = None
    period_end: str | None = None


class InvoiceIn(Schema):
    subscription_id: int
    amount_dh: float = 0


# --- serializers -----------------------------------------------------------
def _plan(p: Plan) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "price_dh": p.price_dh,
        "interval": p.interval,
        "features": p.features or [],
        "is_active": p.is_active,
    }


def _subscription(s: Subscription) -> dict[str, Any]:
    return {
        "id": s.id,
        "username": getattr(s.user, "username", None),
        "plan_id": s.plan_id,
        "plan_name": getattr(s.plan, "name", None),
        "status": s.status,
        "period_start": s.period_start.isoformat() if s.period_start else None,
        "period_end": s.period_end.isoformat() if s.period_end else None,
    }


def _invoice(i: Invoice) -> dict[str, Any]:
    return {
        "id": i.id,
        "subscription_id": i.subscription_id,
        "amount_dh": i.amount_dh,
        "status": i.status,
        "issued_at": i.issued_at.isoformat() if i.issued_at else None,
        "paid_at": i.paid_at.isoformat() if i.paid_at else None,
    }


def _sync_payment_status(user, sub_status: str) -> None:
    """Mirror the subscription status onto the user's payement_status."""
    new_status = "actif" if sub_status == "active" else "suspended"
    if user and getattr(user, "payement_status", None) != new_status:
        user.payement_status = new_status
        user.save(update_fields=["payement_status"])


# --- plans -----------------------------------------------------------------
@router.get("/plans", auth=JwtAuth(), summary="Admin: list plans")
def list_plans(request):
    guard = _require_admin(request)
    if guard:
        return guard
    return [_plan(p) for p in Plan.objects.all().order_by("id")]


@router.post("/plans", auth=JwtAuth(), summary="Admin: create a plan")
def create_plan(request, payload: PlanIn):
    guard = _require_admin(request)
    if guard:
        return guard
    p = Plan.objects.create(**payload.dict())
    record_audit(request.auth, "plan.create", "plan", p.id, payload.dict())
    return _plan(p)


@router.put("/plans/{plan_id}", auth=JwtAuth(), summary="Admin: update a plan")
def update_plan(request, plan_id: int, payload: PlanIn):
    guard = _require_admin(request)
    if guard:
        return guard
    p = Plan.objects.filter(id=plan_id).first()
    if p is None:
        return Response({"detail": "Not found"}, status=404)
    for k, v in payload.dict().items():
        setattr(p, k, v)
    p.save()
    record_audit(request.auth, "plan.update", "plan", p.id, payload.dict())
    return _plan(p)


@router.delete("/plans/{plan_id}", auth=JwtAuth(), summary="Admin: delete a plan")
def delete_plan(request, plan_id: int):
    guard = _require_admin(request)
    if guard:
        return guard
    Plan.objects.filter(id=plan_id).delete()
    record_audit(request.auth, "plan.delete", "plan", plan_id)
    return {"status": "deleted"}


# --- subscriptions ---------------------------------------------------------
@router.get("/subscriptions", auth=JwtAuth(), summary="Admin: list subscriptions")
def list_subscriptions(request, username: str | None = None):
    guard = _require_admin(request)
    if guard:
        return guard
    qs = Subscription.objects.all().order_by("-id")
    if username:
        qs = qs.filter(user__username=username)
    return [_subscription(s) for s in qs]


@router.post("/subscriptions", auth=JwtAuth(), summary="Admin: assign a subscription")
def create_subscription(request, payload: SubscriptionIn):
    guard = _require_admin(request)
    if guard:
        return guard
    user = User.objects.filter(username=payload.username).first()
    if user is None:
        return Response({"detail": "User not found"}, status=404)
    if not Plan.objects.filter(id=payload.plan_id).exists():
        return Response({"detail": "Plan not found"}, status=404)
    sub = Subscription.objects.create(
        user=user,
        plan_id=payload.plan_id,
        status="active",
        period_start=payload.period_start or None,
        period_end=payload.period_end or None,
    )
    _sync_payment_status(user, "active")
    record_audit(
        request.auth, "subscription.create", "subscription", sub.id, payload.dict()
    )
    return _subscription(sub)


@router.post(
    "/subscriptions/{sub_id}/cancel",
    auth=JwtAuth(),
    summary="Admin: cancel a subscription",
)
def cancel_subscription(request, sub_id: int):
    guard = _require_admin(request)
    if guard:
        return guard
    sub = Subscription.objects.filter(id=sub_id).first()
    if sub is None:
        return Response({"detail": "Not found"}, status=404)
    sub.status = "cancelled"
    sub.save(update_fields=["status"])
    _sync_payment_status(sub.user, "cancelled")
    record_audit(request.auth, "subscription.cancel", "subscription", sub.id)
    return _subscription(sub)


# --- invoices --------------------------------------------------------------
@router.get("/invoices", auth=JwtAuth(), summary="Admin: list invoices")
def list_invoices(
    request, username: str | None = None, subscription_id: int | None = None
):
    guard = _require_admin(request)
    if guard:
        return guard
    qs = Invoice.objects.all().order_by("-id")
    if subscription_id:
        qs = qs.filter(subscription_id=subscription_id)
    if username:
        qs = qs.filter(subscription__user__username=username)
    return [_invoice(i) for i in qs]


@router.post("/invoices", auth=JwtAuth(), summary="Admin: create an invoice")
def create_invoice(request, payload: InvoiceIn):
    guard = _require_admin(request)
    if guard:
        return guard
    if not Subscription.objects.filter(id=payload.subscription_id).exists():
        return Response({"detail": "Subscription not found"}, status=404)
    inv = Invoice.objects.create(
        subscription_id=payload.subscription_id, amount_dh=payload.amount_dh
    )
    record_audit(request.auth, "invoice.create", "invoice", inv.id, payload.dict())
    return _invoice(inv)


@router.post(
    "/invoices/{invoice_id}/mark-paid",
    auth=JwtAuth(),
    summary="Admin: mark invoice paid",
)
def mark_invoice_paid(request, invoice_id: int):
    guard = _require_admin(request)
    if guard:
        return guard
    inv = Invoice.objects.filter(id=invoice_id).first()
    if inv is None:
        return Response({"detail": "Not found"}, status=404)
    inv.status = "paid"
    inv.paid_at = timezone.now()
    inv.save(update_fields=["status", "paid_at"])
    record_audit(request.auth, "invoice.mark_paid", "invoice", inv.id)
    return _invoice(inv)
