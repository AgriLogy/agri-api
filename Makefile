.PHONY: all up down build stop start rmi rmv prune stat re \
        bootstrap install hooks dev lint format check test

all: up

# ---- Local Python tooling (uv) -------------------------------------------------

bootstrap: install hooks  ## First-time setup: deps + git hooks

install:  ## Sync the local virtualenv via uv (incl. dev deps)
	cd back && uv sync

hooks:  ## Wire .githooks/ to git so commit-msg + pre-push fire
	./scripts/install-hooks.sh

dev:  ## One-shot: ensure .env, sync, migrate, runserver (SQLite-backed)
	./scripts/dev.sh

lint:  ## ruff check
	cd back && uv run ruff check .

format:  ## ruff format (writes)
	cd back && uv run ruff format .

format-check:  ## ruff format --check (CI-friendly, no writes)
	cd back && uv run ruff format --check .

check: lint format-check  ## Composite gate used by pre-push and CI

test:  ## Django test runner (locmem email backend, in-memory sqlite)
	cd back && SECRET_KEY=ci-test DEBUG=True ALLOWED_HOSTS=* USE_POSTGRES=False \
		uv run python manage.py test --noinput

# ---- Docker compose ------------------------------------------------------------

up:
	@docker compose -f docker-compose.yml up -d

down:
	@docker compose -f docker-compose.yml down

build:
	@docker compose -f docker-compose.yml build

stop:
	@docker compose -f docker-compose.yml stop

start:
	@docker compose -f docker-compose.yml start

rmi:
	@docker rmi $$(docker images -q)

rmv:
	@docker volume rm $$(docker volume ls -q)

prune:
	docker system prune

stat:
	@docker ps

re: down up
