#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys

# src/ layout: the importable packages (agriapi, apps, analytics) live under
# back/src/. Put it on sys.path so `python manage.py ...` works from back/
# without requiring PYTHONPATH (the Docker image also sets PYTHONPATH=/code/src
# for the celery/script entry points that don't go through manage.py).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))


def main():
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agriapi.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
