"""fastapp /feedback — in-app "Report an issue" submissions.

Strangler port of ``apps/feedback/router.py`` (django-ninja). Byte-parity
with the Django version:

* same request schema (title/description/video_url/report_type/metadata),
* same validation (empty description → 400 ``{"detail": ...}``),
* same metadata→column promotion + full blob kept in ``context``,
* same response (201 ``{"id", "status"}``),
* same best-effort internal-team email (never blocks the submission).

The reporter's identity comes from the JWT (never the body). The row is
written to ``feedback_bugreport`` via the agri-core SQLAlchemy session
(no Django ORM); email goes over the Resend HTTPS client in ``fastapp.email``.
"""

from __future__ import annotations

import datetime
import logging
from html import escape

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from agri.core.database.session import session_scope
from agri.db.feedback import FeedbackBugreport
from fastapp import email
from fastapp.auth import AuthedUser, get_current_user
from fastapp.settings import get_settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["feedback"])

MAX_TITLE_LENGTH = 255
MAX_DESCRIPTION_LENGTH = 10000

# Context keys promoted to their own columns for at-a-glance admin triage.
# Everything the client sends is also kept verbatim in ``context``.
_PROMOTED_KEYS = {
    "page_url": ("url", "page_url"),
    "route": ("route",),
    "module": ("detected_module", "module"),
    "environment": ("environment",),
    "app_version": ("app_version",),
    "browser": ("browser",),
    "os": ("os",),
}


class BugReportIn(BaseModel):
    title: str | None = None
    description: str
    video_url: str | None = None
    report_type: str = "bug"
    metadata: dict = {}


def _promote(metadata: dict, aliases: tuple[str, ...]) -> str:
    for key in aliases:
        value = metadata.get(key)
        if value:
            return str(value)[:1000]
    return ""


def _notify_internal(report: FeedbackBugreport) -> None:
    """Email the internal team. Best-effort — never blocks the submission."""
    settings = get_settings()
    recipients = settings.internal_feedback_emails_list
    if not recipients:
        log.info("No INTERNAL_FEEDBACK_EMAILS configured; skipping bug-report email")
        return

    rows = [
        ("Reporter", f"{report.user_email or '—'}"),
        ("Module", report.module or "—"),
        ("Page", report.page_url or report.route or "—"),
        ("Environment", f"{report.environment or '—'} · v{report.app_version or '—'}"),
        ("Browser / OS", f"{report.browser or '—'} · {report.os or '—'}"),
    ]
    context = report.context or {}
    for label, key in (
        ("Viewport", "viewport"),
        ("Screen", "screen_resolution"),
        ("Country", "user_country"),
        ("Timezone", "local_timezone"),
    ):
        if context.get(key):
            rows.append((label, str(context[key])))

    text_lines = [report.description, "", "── Context ──"]
    text_lines += [f"{label}: {value}" for label, value in rows]
    if report.video_url:
        text_lines += ["", f"Recording: {report.video_url}"]
    text_body = "\n".join(text_lines)

    context_html = "".join(
        f"<tr><td style='padding:2px 12px 2px 0;color:#6b7280'>{escape(label)}</td>"
        f"<td style='padding:2px 0'><b>{escape(value)}</b></td></tr>"
        for label, value in rows
    )
    video_html = (
        f"<p><a href='{escape(report.video_url)}'>📹 Watch screen recording</a></p>"
        if report.video_url
        else ""
    )
    html_body = (
        f"<h2 style='margin:0 0 8px'>🐛 {escape(report.title)}</h2>"
        f"<p style='white-space:pre-wrap'>{escape(report.description)}</p>"
        f"{video_html}"
        f"<table style='border-collapse:collapse;font-size:14px;margin-top:12px'>"
        f"{context_html}</table>"
        f"<p style='color:#9ca3af;font-size:12px;margin-top:16px'>"
        f"Report #{report.id} · agri-front “Report an issue”</p>"
    )

    email.send_email(
        api_key=settings.resend_api_key,
        from_email=settings.default_from_email,
        to=recipients,
        subject=f"🐛 [{report.module or 'Report'}] {report.title}",
        text=text_body,
        html=html_body,
    )


@router.post("/feedback", status_code=201, summary='Submit an in-app "Report an issue"')
def submit_report(
    payload: BugReportIn,
    user: AuthedUser = Depends(get_current_user),
):
    description = (payload.description or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description is required")
    description = description[:MAX_DESCRIPTION_LENGTH]

    title = (payload.title or "").strip() or description
    title = title[:MAX_TITLE_LENGTH]

    metadata = payload.metadata or {}

    with session_scope(commit=True) as session:
        report = FeedbackBugreport(
            report_type=(payload.report_type or "bug")[:20],
            title=title,
            description=description,
            # Match the Django model default explicitly (server_default 'open'
            # isn't populated on the instance before a refresh).
            status="open",
            video_url=(payload.video_url or "")[:1000],
            user_id=user.id,
            user_email=(user.email or "")[:254],
            page_url=_promote(metadata, _PROMOTED_KEYS["page_url"]),
            route=_promote(metadata, _PROMOTED_KEYS["route"])[:255],
            module=_promote(metadata, _PROMOTED_KEYS["module"])[:100],
            environment=_promote(metadata, _PROMOTED_KEYS["environment"])[:50],
            app_version=_promote(metadata, _PROMOTED_KEYS["app_version"])[:50],
            browser=_promote(metadata, _PROMOTED_KEYS["browser"])[:255],
            os=_promote(metadata, _PROMOTED_KEYS["os"])[:100],
            context=metadata,
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )
        session.add(report)
        session.flush()  # assign the PK before we read it
        result = {"id": report.id, "status": report.status}
        _notify_internal(report)

    return result
