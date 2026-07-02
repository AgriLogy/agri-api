"""Unmanaged ORM view of ``feedback_bugreport`` (owned by agri-db).

Mirrors ``agri.db.feedback.FeedbackBugreport``. ``managed = False`` — the table
is created/altered exclusively by agri-db Alembic migrations. Registering the
model here makes it appear (with full CRUD) in the generic ``/api/admin/db``
back-office, and lets the ``/feedback`` router persist rows via the ORM.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models


class BugReport(models.Model):
    STATUS_CHOICES = [
        ("open", "Open"),
        ("in_progress", "In progress"),
        ("resolved", "Resolved"),
        ("closed", "Closed"),
    ]

    report_type = models.CharField(max_length=20, default="bug")
    title = models.CharField(max_length=255)
    description = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    video_url = models.CharField(max_length=1000, blank=True, default="")

    # Reporter — nullable so a report survives if the account is later removed.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="user_id",
        related_name="bug_reports",
    )
    user_email = models.CharField(max_length=254, blank=True, default="")

    # Client context (denormalised for at-a-glance triage in the admin).
    page_url = models.CharField(max_length=1000, blank=True, default="")
    route = models.CharField(max_length=255, blank=True, default="")
    module = models.CharField(max_length=100, blank=True, default="")
    environment = models.CharField(max_length=50, blank=True, default="")
    app_version = models.CharField(max_length=50, blank=True, default="")
    browser = models.CharField(max_length=255, blank=True, default="")
    os = models.CharField(max_length=100, blank=True, default="")
    # Full metadata blob (everything the client sent, incl. fields above).
    context = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)

    class Meta:
        app_label = "feedback"
        db_table = "feedback_bugreport"
        managed = False
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"[{self.status}] {self.title}"
