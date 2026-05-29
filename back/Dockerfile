FROM python:3.12.8-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:/root/.local/bin:$PATH

WORKDIR /code

# System packages: cron at runtime; build tooling for psycopg2 wheel fallbacks;
# git so uv can resolve `agri-core @ git+https://...` and other VCS deps.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        cron \
        build-essential \
        libpq-dev \
        git \
    && rm -rf /var/lib/apt/lists/*

# Install uv (pinned for reproducibility).
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /usr/local/bin/

# Install dependencies first so layer caching survives source edits.
# agri-core transitively pulls the PRIVATE agri-db. Mount AGRI_DB_RO_TOKEN as
# a BuildKit secret and configure it as a git credential so `uv sync` can
# fetch agri-db. The `if -f` guard keeps the build working when no token is
# supplied (e.g. before agri-core pins agri-db).
# Build with: docker build --secret id=agri_db_token,env=AGRI_DB_RO_TOKEN ...
# (docker-compose passes it via the build `secrets:` block).
COPY pyproject.toml uv.lock* /code/
RUN --mount=type=secret,id=agri_db_token \
    if [ -s /run/secrets/agri_db_token ]; then \
        git config --global \
            url."https://x-access-token:$(cat /run/secrets/agri_db_token)@github.com/".insteadOf \
            "https://github.com/"; \
    fi \
    && (uv sync --frozen --no-install-project --no-dev \
        || uv sync --no-install-project --no-dev)

# Project source.
COPY . /code/

# Entrypoint scripts. docker-entrypoint.sh dispatches by role
COPY docker-entrypoint.sh /code/docker-entrypoint.sh
COPY env-example /.env
RUN chmod +x /code/docker-entrypoint.sh

RUN mkdir -m 777 /shared

CMD ["bash", "/code/docker-entrypoint.sh", "web"]
