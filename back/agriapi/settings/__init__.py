"""Resolve the active Django settings module from DJANGO_ENV.

Reading order:
  - DJANGO_ENV env var (preferred)
  - DJANGO_SETTINGS_VARIANT env var (legacy alias)
  - default: "dev"

The submodule is imported via `from .<env> import *` so all
Django-required globals (DATABASES, SECRET_KEY, ALLOWED_HOSTS, ...)
end up on this package.
"""

from __future__ import annotations

import os

env = (
    os.environ.get("DJANGO_ENV") or os.environ.get("DJANGO_SETTINGS_VARIANT") or "dev"
).lower()

if env == "prod":
    from .prod import *  # noqa: F401, F403
elif env == "test":
    from .test import *  # noqa: F401, F403
elif env == "dev":
    from .dev import *  # noqa: F401, F403
else:
    raise RuntimeError(f"Unknown DJANGO_ENV={env!r}. Use one of: dev, prod, test.")
