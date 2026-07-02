"""In-app "Report an issue" endpoint.

``POST /feedback`` — a farmer (or technician/staff) submits a bug report from
the web app. The report is persisted to ``feedback_bugreport`` (visible in the
admin back-office) and a notification email is sent to the internal team
(``settings.INTERNAL_FEEDBACK_EMAILS`` — the ``internal_*`` addresses).

The reporter's identity (id / email / role) is taken from the JWT server-side,
never trusted from the request body. The client only supplies the free-text
report, an optional Cloudinary recording URL, and client-observable context
(route, browser, OS, viewport, app version, ...).
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils.html import escape
from ninja import Router, Schema
from ninja.responses import Response

from agriapi.api.auth import JwtAuth
from apps.feedback.models import BugReport

log = logging.getLogger(__name__)

router = Router()

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


class BugReportIn(Schema):
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


def _notify_internal(report: BugReport) -> None:
    """Email the internal team. Best-effort — never blocks the submission."""
    recipients = getattr(settings, "INTERNAL_FEEDBACK_EMAILS", [])
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

    try:
        send_mail(
            subject=f"🐛 [{report.module or 'Report'}] {report.title}",
            message=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=list(recipients),
            html_message=html_body,
            fail_silently=False,
        )
        log.info("Sent bug-report #%s email to %s", report.id, recipients)
    except Exception:  # noqa: BLE001 — email must never fail the submission
        log.exception("Failed to email bug-report #%s to internal team", report.id)


@router.post("", auth=JwtAuth(), summary='Submit an in-app "Report an issue"')
def submit_report(request, payload: BugReportIn):
    description = (payload.description or "").strip()
    if not description:
        return Response({"detail": "description is required"}, status=400)
    description = description[:MAX_DESCRIPTION_LENGTH]

    title = (payload.title or "").strip() or description
    title = title[:MAX_TITLE_LENGTH]

    user = request.auth
    metadata = payload.metadata or {}

    report = BugReport.objects.create(
        report_type=(payload.report_type or "bug")[:20],
        title=title,
        description=description,
        video_url=(payload.video_url or "")[:1000],
        user=user,
        user_email=(getattr(user, "email", "") or "")[:254],
        page_url=_promote(metadata, _PROMOTED_KEYS["page_url"]),
        route=_promote(metadata, _PROMOTED_KEYS["route"])[:255],
        module=_promote(metadata, _PROMOTED_KEYS["module"])[:100],
        environment=_promote(metadata, _PROMOTED_KEYS["environment"])[:50],
        app_version=_promote(metadata, _PROMOTED_KEYS["app_version"])[:50],
        browser=_promote(metadata, _PROMOTED_KEYS["browser"])[:255],
        os=_promote(metadata, _PROMOTED_KEYS["os"])[:100],
        context=metadata,
    )

    _notify_internal(report)

    return Response({"id": report.id, "status": report.status}, status=201)
