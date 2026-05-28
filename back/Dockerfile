FROM python:3.12.8-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:/root/.local/bin:$PATH

WORKDIR /code

# System packages: cron at runtime; build tooling for psycopg2 wheel fallbacks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        cron \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install uv (pinned for reproducibility).
COPY --from=ghcr.io/astral-sh/uv:0.5.7 /uv /uvx /usr/local/bin/

# Install dependencies first so layer caching survives source edits.
COPY pyproject.toml uv.lock* /code/
RUN uv sync --frozen --no-install-project --no-dev \
    || uv sync --no-install-project --no-dev

# Project source.
COPY . /code/

# Entrypoint scripts. docker-entrypoint.sh dispatches by role
COPY docker-entrypoint.sh /code/docker-entrypoint.sh
COPY env-example /.env
RUN chmod +x /code/docker-entrypoint.sh

RUN mkdir -m 777 /shared

CMD ["bash", "/code/docker-entrypoint.sh", "web"]
