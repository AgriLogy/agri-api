"""fastapp assistant — strangler port of the Django ``apps.assistant`` package.

Mirrors the Django layout one-to-one (registry / tools / orchestrator / llm),
but every DB access goes through the agri-core SQLAlchemy session instead of
the Django ORM. The tool catalog, the rule-based orchestrator's routing, and
the conversation CRUD are all deterministic → byte-identical to django-ninja.
"""
