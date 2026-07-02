from django.apps import AppConfig


class FeedbackConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.feedback"
    # Own app label "feedback" → the model surfaces as ``feedback.bugreport``
    # in the generic /api/admin/db CRUD. The table itself is owned by agri-db
    # (feedback_bugreport); the model is unmanaged (Meta.managed = False), so
    # Django never issues DDL for it.
    label = "feedback"
