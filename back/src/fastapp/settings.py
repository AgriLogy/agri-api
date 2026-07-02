"""fastapp runtime settings — the SAME env vars Django reads, via pydantic.

One env, two processes: every name here is already consumed by the Django
settings package (``agriapi/settings/base.py`` + ``dev.py`` / ``prod.py``),
so a droplet/.env change reconfigures both the Django app and this sidecar.
NO new variable names are introduced.

Defaults mirror the Django *dev* defaults; production supplies everything
through ``back/.env`` (compose ``env_file``) exactly as it does for Django.
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# back/src/fastapp/settings.py → back/ (same anchor as Django's BASE_DIR).
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Matches agriapi.settings.dev's CORS_ALLOWED_ORIGINS default: local dev ports
# of the app family (web :3000, admin :3001, identity :3002) + the deployed
# web origins. Prod overrides via the CORS_ALLOWED_ORIGINS env var, which the
# Django prod settings already require.
_DEV_CORS_DEFAULT = (
    "http://localhost:3000,"
    "http://127.0.0.1:3000,"
    "http://localhost:3001,"
    "http://127.0.0.1:3001,"
    "http://localhost:3002,"
    "http://127.0.0.1:3002,"
    "http://157.245.43.196:3000,"
    "https://157.245.43.196:3000,"
    "https://www.agrogo-datafarm.com"
)


def _read_project_version() -> str:
    """Version of the deployed code = back/pyproject.toml's project.version
    (kept current by python-semantic-release). Falls back to 0.0.0 when the
    file isn't readable (e.g. an exotic packaging layout)."""
    try:
        with open(BASE_DIR / "pyproject.toml", "rb") as fh:
            return tomllib.load(fh)["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "0.0.0"


class AppSettings(BaseSettings):
    """Environment-driven settings, one field per Django-consumed env var."""

    model_config = SettingsConfigDict(
        # Same file Django's load_dotenv reads (back/.env); real env wins.
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # -- Core ---------------------------------------------------------------
    # Django: SECRET_KEY (signs the simplejwt HS256 tokens fastapp verifies).
    secret_key: str = Field(
        default="dev-not-secret-please-override", alias="SECRET_KEY"
    )
    # Django: DJANGO_ENV selects dev|prod|test settings; fastapp only branches
    # on "prod" (strict CORS expectations, no permissive defaults).
    django_env: str = Field(default="dev", alias="DJANGO_ENV")

    # -- Database (same POSTGRES_* / AGRI_DB_URL contract as Django) ---------
    postgres_db: str = Field(default="postgres", alias="POSTGRES_DB")
    postgres_user: str = Field(default="postgres", alias="POSTGRES_USER")
    postgres_password: str = Field(default="", alias="POSTGRES_PASSWORD")
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    # Canonical SQLAlchemy DSN consumed by agri-core. When unset, it is
    # derived from the POSTGRES_* parts — the exact mirror of Django's
    # export_agri_db_url() so both ORMs hit the same Postgres.
    agri_db_url: str = Field(default="", alias="AGRI_DB_URL")

    # -- Celery / Redis -------------------------------------------------------
    celery_broker_url: str = Field(
        default="redis://redis:6379/0", alias="CELERY_BROKER_URL"
    )

    # -- Email (Resend HTTP backend — droplet blocks outbound SMTP) ----------
    resend_api_key: str = Field(default="", alias="RESEND_API_KEY")
    default_from_email: str = Field(
        default="Agrilogy <noreply@agrogo-datafarm.com>", alias="DEFAULT_FROM_EMAIL"
    )

    # -- CORS (comma-separated origin list, django-cors-headers style) -------
    cors_allowed_origins: str = Field(
        default=_DEV_CORS_DEFAULT, alias="CORS_ALLOWED_ORIGINS"
    )

    # -- Derived --------------------------------------------------------------
    version: str = Field(default_factory=_read_project_version)

    @property
    def is_prod(self) -> bool:
        return self.django_env.lower() == "prod"

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        """Split like Django settings' _csv_env (strip + drop empties)."""
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def database_url(self) -> str:
        """The SQLAlchemy DSN agri-core should use. Mirrors Django's
        export_agri_db_url(): explicit AGRI_DB_URL wins, else it is built
        from the POSTGRES_* parts."""
        if self.agri_db_url:
            return self.agri_db_url
        return (
            "postgresql+psycopg://"
            f"{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Process-wide settings singleton (cached — env is read once)."""
    return AppSettings()
