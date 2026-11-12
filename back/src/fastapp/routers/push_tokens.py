"""Mobile push registration (agri-api #37).

The apps register the FCM token of the install after the user grants
notification permission, and unregister it on sign-out:

* ``POST   /devices/push-tokens``  {token, platform, app_version?} → upsert.
  A token already registered under another account moves to the caller (the
  last signed-in user owns the device); re-registering refreshes
  ``last_seen_at``.
* ``DELETE /devices/push-tokens``  {token} → remove it if it is the caller's.
* ``GET    /devices/push-tokens``  → the caller's devices (token tails only)
  and whether the server can send push at all (``push_configured``).

Any authenticated user may call these; they only ever touch the caller's rows.
Registered before ``devices.router`` so ``/devices/push-tokens`` is never
mistaken for ``/devices/{pk}``.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from agri.core.database import session_scope
from agri.db.notifications import PUSH_PLATFORMS, NotificationPushToken
from fastapp import push
from fastapp.auth import AuthedUser, get_current_user
from fastapp.json import DjangoStyleJSONResponse

router = APIRouter(tags=["push"])

BASE = "/devices/push-tokens"


class PushTokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    platform: str
    app_version: str = Field(default="", max_length=32)


class PushTokenRef(BaseModel):
    token: str = Field(min_length=1, max_length=512)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _tail(token: str) -> str:
    return "…" + token[-8:]


@router.post(BASE, summary="Register this install's FCM token for push")
def register_push_token(
    payload: PushTokenIn, user: AuthedUser = Depends(get_current_user)
):
    token = payload.token.strip()
    if not token:
        return DjangoStyleJSONResponse(
            {"detail": "token is required."}, status_code=400
        )
    if payload.platform not in PUSH_PLATFORMS:
        return DjangoStyleJSONResponse(
            {"detail": f"platform must be one of {list(PUSH_PLATFORMS)}."},
            status_code=400,
        )
    now = _now()
    with session_scope(commit=True) as session:
        existed = (
            session.scalar(
                select(NotificationPushToken.id).where(
                    NotificationPushToken.token == token
                )
            )
            is not None
        )
        stmt = pg_insert(NotificationPushToken).values(
            user_id=user.id,
            token=token,
            platform=payload.platform,
            app_version=payload.app_version.strip(),
            created_at=now,
            last_seen_at=now,
        )
        row_id = session.scalar(
            stmt.on_conflict_do_update(
                constraint="notification_push_token_token_key",
                set_={
                    "user_id": stmt.excluded.user_id,
                    "platform": stmt.excluded.platform,
                    "app_version": stmt.excluded.app_version,
                    "last_seen_at": stmt.excluded.last_seen_at,
                },
            ).returning(NotificationPushToken.id)
        )
    return DjangoStyleJSONResponse(
        {
            "id": row_id,
            "platform": payload.platform,
            "created": not existed,
            "push_configured": push.is_configured(),
        },
        status_code=200 if existed else 201,
    )


@router.delete(BASE, summary="Unregister this install's FCM token")
def unregister_push_token(
    payload: PushTokenRef, user: AuthedUser = Depends(get_current_user)
):
    with session_scope(commit=True) as session:
        session.execute(
            delete(NotificationPushToken).where(
                NotificationPushToken.token == payload.token.strip(),
                NotificationPushToken.user_id == user.id,
            )
        )
    # Idempotent: unknown / someone else's token → still 204, nothing leaked.
    return DjangoStyleJSONResponse(None, status_code=204)


@router.get(BASE, summary="List the caller's push-registered devices")
def list_push_tokens(user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        rows = session.scalars(
            select(NotificationPushToken)
            .where(NotificationPushToken.user_id == user.id)
            .order_by(NotificationPushToken.last_seen_at.desc())
        ).all()
        devices = [
            {
                "id": r.id,
                "platform": r.platform,
                "app_version": r.app_version,
                "token_tail": _tail(r.token),
                "created_at": r.created_at.isoformat(),
                "last_seen_at": r.last_seen_at.isoformat(),
            }
            for r in rows
        ]
    return {"push_configured": push.is_configured(), "devices": devices}
