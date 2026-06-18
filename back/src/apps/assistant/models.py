"""Server-side conversation history for the assistant.

One row per conversation, with the messages stored as a JSON list (chat logs
are append-mostly and never queried relationally, so a normalized message table
would be overkill). `client_id` is the frontend-generated id, so a conversation
keeps a stable identity whether it was first created offline (localStorage) or
on the server — sync upserts by (user, client_id).

Like ``LoraUplink``, this table is created out-of-band (see
``scripts/ensure_assistant_tables.py``, run on web boot) rather than by a
migration, since schema-of-record lives in agri-db.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models


class AssistantConversation(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="assistant_conversations",
    )
    # Frontend-generated conversation id (stable across devices/offline).
    client_id = models.CharField(max_length=64)
    title = models.CharField(max_length=200, blank=True, default="")
    # [{id, role, content, card?, timestamp}, ...]
    messages = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        app_label = "assistant"
        db_table = "assistant_conversation"
        # Django does NOT manage this table: it's created out-of-band
        # (scripts/ensure_assistant_tables.py on boot / a test fixture), since
        # schema-of-record lives in agri-db. managed=False keeps it out of the
        # migration graph AND out of the test flush table-list.
        managed = False
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "client_id"], name="uniq_user_client_conversation"
            )
        ]
        indexes = [models.Index(fields=["user", "-updated_at"])]

    def __str__(self) -> str:
        return f"Conversation({self.client_id} · user {self.user_id})"
