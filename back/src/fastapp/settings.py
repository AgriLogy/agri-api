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

    # -- Logging (NEW env vars, read by both processes) ----------------------
    # LOG_LEVEL: root log level (DEBUG|INFO|WARNING|ERROR). LOG_FORMAT: "json"
    # for one-object-per-line (Promtail/Loki) or "text" for human-readable
    # local dev. Defaults to structured JSON so a droplet needs no extra env
    # to feed the observability stack; set LOG_FORMAT=text locally if desired.
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: str = Field(default="json", alias="LOG_FORMAT")

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
    # Internal team recipients for in-app bug reports (apps.feedback). Same
    # env var + default the Django settings expose (INTERNAL_FEEDBACK_EMAILS).
    internal_feedback_emails: str = Field(
        default=(
            "internal_zakaria@agrogo-datafarm.com,"
            "internal_anass@agrogo-datafarm.com,"
            "internal_brahim@agrogo-datafarm.com"
        ),
        alias="INTERNAL_FEEDBACK_EMAILS",
    )

    # -- Assistant LLM orchestrator (OpenAI-compatible chat-completions) ------
    # Same env vars agriapi.settings.base reads. When AI_API_KEY is empty the
    # assistant falls back to the deterministic rule-based orchestrator (so the
    # sidecar behaves exactly like Django with no key configured).
    ai_api_key: str = Field(default="", alias="AI_API_KEY")
    ai_api_base_url: str = Field(
        default="https://api.groq.com/openai/v1", alias="AI_API_BASE_URL"
    )
    ai_model: str = Field(default="llama-3.3-70b-versatile", alias="AI_MODEL")
    ai_timeout: int = Field(default=20, alias="AI_TIMEOUT")

    # -- CORS (comma-separated origin list, django-cors-headers style) -------
    cors_allowed_origins: str = Field(
        default=_DEV_CORS_DEFAULT, alias="CORS_ALLOWED_ORIGINS"
    )

    # -- Irrigation automation -----------------------------------------------
    # Django: IRRIGATION_DISPATCH_ENABLED gates physical valve/pump actuation
    # (off = commands are simulated). Same env var + truthy parsing.
    irrigation_dispatch_enabled_raw: str = Field(
        default="false", alias="IRRIGATION_DISPATCH_ENABLED"
    )

    # -- MQTT ingest (fastapp-native transport; NOT mirrored from Django) -----
    # Unlike every field above, these are NEW variable names: MQTT ingest is a
    # fastapp-only process (``docker-entrypoint.sh mqtt`` → ``fastapp.mqtt``),
    # so there is no Django reader to stay in lock-step with. The subscriber
    # connects to ChirpStack's existing MQTT broker (LoRaWAN uplinks land there
    # natively) and to a generic ``{prefix}/{client}/...`` topic tree. Dev
    # defaults target the compose ``mosquitto`` container; the droplet supplies
    # broker host/creds via back/.env. An empty host disables the subscriber.
    mqtt_host: str = Field(default="mosquitto", alias="MQTT_HOST")
    mqtt_port: int = Field(default=1883, alias="MQTT_PORT")
    mqtt_username: str = Field(default="", alias="MQTT_USERNAME")
    mqtt_password: str = Field(default="", alias="MQTT_PASSWORD")
    mqtt_tls_raw: str = Field(default="false", alias="MQTT_TLS")
    mqtt_client_id: str = Field(default="agri-api-ingest", alias="MQTT_CLIENT_ID")
    mqtt_qos: int = Field(default=1, alias="MQTT_QOS")
    # Topic prefix for the generic/Bivocom sensor tree: {prefix}/{client}/...
    mqtt_topic_prefix: str = Field(default="agrilogy", alias="MQTT_TOPIC_PREFIX")
    # ChirpStack v4 publishes uplinks here by default (application/{id}/device/
    # {devEui}/event/up). Override if the tenant uses a non-default topic.
    mqtt_chirpstack_topic: str = Field(
        default="application/+/device/+/event/up", alias="MQTT_CHIRPSTACK_TOPIC"
    )
    # Liveness file the subscriber keeps fresh WHILE connected to the broker (see
    # fastapp.mqtt). The container healthcheck stats it, so `docker ps` reports
    # unhealthy whenever the broker connection is down — the process being alive
    # is NOT evidence that uplinks are being consumed.
    mqtt_health_file: str = Field(default="/tmp/mqtt-healthy", alias="MQTT_HEALTH_FILE")
    # How often the heartbeat thread re-touches that file, seconds. The
    # healthcheck's staleness window must be a comfortable multiple of this.
    mqtt_health_interval: int = Field(default=15, alias="MQTT_HEALTH_INTERVAL")

    # -- Derived --------------------------------------------------------------
    version: str = Field(default_factory=_read_project_version)

    @property
    def is_prod(self) -> bool:
        return self.django_env.lower() == "prod"

    @property
    def irrigation_dispatch_enabled(self) -> bool:
        """Mirror Django base.py: truthy iff '1' / 'true' / 'yes'."""
        return self.irrigation_dispatch_enabled_raw.lower() in ("1", "true", "yes")

    @property
    def mqtt_tls(self) -> bool:
        """TLS to the MQTT broker — truthy iff '1' / 'true' / 'yes'."""
        return self.mqtt_tls_raw.lower() in ("1", "true", "yes")

    @property
    def mqtt_enabled(self) -> bool:
        """Subscriber runs only when a broker host is configured."""
        return bool(self.mqtt_host.strip())

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        """Split like Django settings' _csv_env (strip + drop empties)."""
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def internal_feedback_emails_list(self) -> list[str]:
        """Split like Django settings' _csv_env (strip + drop empties)."""
        return [
            e.strip() for e in self.internal_feedback_emails.split(",") if e.strip()
        ]

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
