# Staging / preprod stack — runbook

> ## ⚠️ NOTHING IN THIS DIRECTORY HAS BEEN APPLIED ANYWHERE.
>
> No DNS record was created, no certificate was issued, no container was
> started, no database exists, no nginx config was copied to the droplet, no
> Vercel project was touched, and no CI workflow was changed. These are
> **definition files only**. Every command below is for a human to run
> deliberately, in order, after the owner-gated prerequisites are satisfied.

A second, fully isolated copy of the API stack on the **same droplet** as
production, reachable at `staging.agrogo-datafarm.com`. Its purpose is to prove
a change — especially a **schema migration** — before it reaches prod.

```
 staging.agrogo-datafarm.com ─ nginx TLS ─┬─ 127.0.0.1:9001  agri-api-fast-staging   (fastapp)
                                          └─ 127.0.0.1:9000  agri-api-web-staging    (Django)
                                                  │
                                        agro-staging network
                                                  │
                    ┌─────────────────┬───────────┴──────────┬──────────────────┐
              agrydata-staging   redis-staging        agri-api-worker/beat   mailpit-staging
              (own volume)       (own broker)         -staging               (email sink)
```

Files:

| File | What it is |
|---|---|
| `docker-compose.staging.yml` | The stack. Separate from the prod `docker-compose.yml`, which is untouched. |
| `env.example` | Env template — placeholders only, prod-vs-staging differences annotated. |
| `../nginx/staging.conf` | nginx server block, a mechanical transform of `../nginx/back.conf`. |

---

## 0. Prerequisites — GATED ON THE OWNER

None of these can be done from the repo. Work stops here until each is signed
off.

| # | Gate | Why it is blocking | Who |
|---|---|---|---|
| 0.1 | **DNS A record** `staging.agrogo-datafarm.com` → `157.245.43.196` | certbot cannot issue without it resolving | Owner (DNS registrar) |
| 0.2 | **Hostname decision**: does `staging.` belong to the API or to the staging front-end? | The original plan sketched `staging.` for the Vercel app and `staging-back.` for the API. Issue #425 asks for the API on `staging.`. **Both cannot own it.** Changing it later means re-issuing the cert. | Owner |
| 0.3 | **TLS issuance** — `certbot --nginx -d staging.agrogo-datafarm.com` on the droplet | Needs root SSH + 0.1 done | Owner |
| 0.4 | **Vercel staging project(s)** + their env vars (`NEXT_PUBLIC_API_URL` → the staging API host) for `agri-web` (and `agri-admin` / `agri-identity` if they are to have staging too) | Cannot be created from this repo; the prod outage in `agri-web` was caused by a bad `NEXT_PUBLIC_API_URL`, so this must be an **absolute** URL | Owner (Vercel) |
| 0.5 | **Branch-strategy sign-off**: `main` → staging, promotion to prod via a `production` branch/tag | Changes what a merge to `main` does today (it deploys straight to prod). Until signed off, **do not** modify `.github/workflows/deploy-back.yml` — this task deliberately did not. | Owner |
| 0.6 | **Droplet headroom check** — a second app stack **plus a second Postgres** on one droplet | RAM/disk. Check `free -h` / `df -h` before bring-up; the staging Postgres volume grows independently. | Owner |
| 0.7 | **Firewall** — the droplet has none. Every staging port here binds `127.0.0.1` for that reason. If a DO Cloud Firewall is ever added, keep 9000/9001/9432/9379/9025/9026 closed to the internet. | Security | Owner |
| 0.8 | **Staging DB seed policy**: fresh empty DB, or a restored prod dump? | A prod dump brings REAL customer emails/phone numbers into staging. If chosen, scrub contact fields (see §3c). | Owner |

---

## 1. Bring-up

Staging gets its **own checkout** so a prod deploy (`git reset` in
`/root/agri-api`) can never touch it.

```bash
# on the droplet, as root
git clone https://github.com/AgriLogy/agri-api.git /root/agri-api-staging
cd /root/agri-api-staging
git checkout main            # staging tracks main; prod tracks the promoted ref

cd deploy/staging
cp env.example .env.staging
$EDITOR .env.staging         # fill EVERY placeholder — read the [≠PROD] comments

export AGRI_DB_RO_TOKEN=...  # read-only PAT for the private agri-db (build secret)

# Postgres + Redis + mailpit first, so the schema can be applied before the app
# containers boot against an empty DB.
docker compose --env-file .env.staging -f docker-compose.staging.yml \
  up -d agrydata-staging redis-staging mailpit-staging
```

`--env-file .env.staging` is required: compose auto-loads `.env` for `${...}`
interpolation, not `.env.staging`, and the Postgres service uses interpolation.

---

## 2. Database creation

The `agrydata-staging` container creates the database, user and password from
`POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` on **first** start only —
they are baked into the volume. Getting them wrong means
`docker compose ... down -v` and starting over.

```bash
docker exec -it agrydata-staging \
  psql -U agrilogy_staging -d agrilogy_staging -c '\l'
```

---

## 3. Migrations — Alembic ONLY

> **The schema is owned by `agri-db` (Alembic). Never run
> `python manage.py migrate` or `makemigrations`.** Django's entrypoint already
> skips migrate on boot. This rule holds on staging exactly as on prod.

### 3a. Fresh, empty staging DB (recommended)

Replay the whole Alembic history from base — this is the single best reason for
staging to exist, because it proves the migration chain actually applies.

```bash
cd /root/agri-api-staging/deploy/staging
docker compose --env-file .env.staging -f docker-compose.staging.yml \
  run --rm agri-api-web-staging bash /code/docker-entrypoint.sh migrate
```

The `migrate` role is gated by `RUN_DB_MIGRATIONS`, which `env.example` sets to
`true` for staging (prod keeps it `false` — see `docs/MIGRATIONS_PROD_CUTOVER.md`).
On a DB with no `alembic_version` table **and** no tables, leave
`ALEMBIC_STAMP_REV` empty and let it upgrade from base.

### 3b. Restored prod dump

A dump has every table but its `alembic_version` row may not match the pinned
`agri-db`. Set `ALEMBIC_STAMP_REV` **once** to the revision that matches the
restored schema, run the migrate role, then blank it again and re-run to apply
anything newer. Never guess the revision.

### 3c. If you restored a prod dump — scrub before starting the app

Staging Celery beat will start evaluating alerts as soon as it boots.

```sql
-- run BEFORE `up -d` of the app containers
UPDATE customuser SET email = 'staging+' || id || '@example.invalid';
UPDATE customuser SET phone_number = NULL;   -- adjust to the real column names
```

Mailpit already contains the email blast radius; the phone scrub matters
because Twilio does not pass through mailpit (keep `TWILIO_*` empty regardless).

### 3d. Start the app containers

```bash
docker compose --env-file .env.staging -f docker-compose.staging.yml \
  up -d --build
```

`--build` produces **`agri-api:staging`**, a distinct tag. It never overwrites
`agri-api:latest`, which is what every prod container would pick up on its next
recreate.

The MQTT subscriber is behind the `mqtt` profile and is **not** started. See
§6 before ever enabling it.

---

## 4. nginx

```bash
sudo cp /root/agri-api-staging/deploy/nginx/staging.conf \
        /etc/nginx/sites-available/staging
sudo ln -sf /etc/nginx/sites-available/staging /etc/nginx/sites-enabled/staging
# The file ships with the certbot-managed 443 block. Comment out `listen 443`
# and the four ssl_* lines until the cert exists, or nginx -t will fail on a
# missing certificate.
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d staging.agrogo-datafarm.com
sudo nginx -t && sudo systemctl reload nginx
```

`staging.conf` is a mechanical transform of `back.conf` (`:8001`→`:9001`,
`:8000`→`:9000`, hostname, cert paths). Every `location` block is present in the
same order with the same matching modifier, so staging routes identically to
prod. When a cutover adds a block to `back.conf`, regenerate rather than
hand-edit — the recipe and the verification `diff` are in the file header.

---

## 5. Verification checklist

Run all of it. The last three items are the ones that prove prod is safe.

```bash
# --- staging is alive -------------------------------------------------------
curl -sS -o /dev/null -w '%{http_code}\n' https://staging.agrogo-datafarm.com/admin/login/   # 200 → Django up
curl -sS https://staging.agrogo-datafarm.com/healthz                                          # fastapp :9001 via nginx
curl -sS -X POST https://staging.agrogo-datafarm.com/auth/token/ \
     -d 'username=...&password=...'                                                           # auth path end-to-end

# --- routing parity with prod ------------------------------------------------
# A strangled prefix must land on fastapp on BOTH hosts.
curl -sS -o /dev/null -w '%{http_code}\n' https://staging.agrogo-datafarm.com/sensors
diff <(grep -E '^\s+location ' /root/agri-api/deploy/nginx/back.conf) \
     <(grep -E '^\s+location ' /root/agri-api-staging/deploy/nginx/staging.conf)   # must be empty

# --- containers + celery ------------------------------------------------------
docker compose --env-file .env.staging -f docker-compose.staging.yml ps
# every service Up; web healthy. If worker/beat sit in `created`, that is the
# known deploy-strands-Celery failure → `docker start agri-api-worker-staging`.
docker logs --tail 50 agri-api-worker-staging   # "celery@... ready", queue agriapi
docker logs --tail 50 agri-api-beat-staging     # ticks on the static schedule

# --- ISOLATION PROOFS (the important ones) -----------------------------------
# 1. Staging cannot even resolve prod's DB / broker.
docker exec agri-api-web-staging getent hosts agrydata   # MUST return nothing
docker exec agri-api-web-staging getent hosts redis      # MUST return nothing

# 2. Staging writes to the staging DB only.
docker exec agrydata-staging psql -U agrilogy_staging -d agrilogy_staging \
  -c 'select count(*) from django_session'                # grows as you use staging
# ...and the prod row counts do not move while you exercise staging.

# 3. Nothing staging touched prod's containers.
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'agri-api-(web|fast|worker|beat|mqtt)$'
# prod uptimes UNCHANGED (no restart) after the staging bring-up.

# 4. The prod image tag was not rebuilt.
docker images agri-api --format '{{.Tag}}\t{{.ID}}\t{{.CreatedSince}}'
# `latest` CreatedSince must be older than the staging bring-up; `staging` new.

# 5. Emails are captured, not sent.
#    ssh -L 9025:127.0.0.1:9025 root@157.245.43.196 → http://localhost:9025
#    Trigger a staging alert and confirm the message lands in mailpit.

# 6. Nothing staging binds a public port.
ss -ltnp | grep -E ':(9000|9001|9025|9026|9379|9432)\b'   # all 127.0.0.1, never 0.0.0.0
```

---

## 6. MQTT — off by default, and why

`agri-api-mqtt-staging` is behind the `mqtt` compose profile, so a plain
`up -d` does not start it, and `MQTT_HOST` is empty in `env.example` (which makes
`fastapp.mqtt` log `mqtt.disabled` and idle even if the container is started).

Three hazards, in severity order:

1. **Client-id collision — this one actually breaks production.** An MQTT broker
   allows one connection per client id; a second client using an id already in
   use causes the broker to **disconnect the first**. `fastapp` defaults
   `MQTT_CLIENT_ID` to `agri-api-ingest`. A staging subscriber on that default,
   pointed at the prod ChirpStack broker, would repeatedly kick the prod
   subscriber offline — the two flap and **prod uplinks are lost**.
   `env.example` pins `MQTT_CLIENT_ID=agri-api-ingest-staging`.
2. **Staging does not *steal* prod messages, it duplicates them.** MQTT without
   shared subscriptions (which `fastapp.mqtt` does not use) fans out: every
   subscriber receives every matching message. So prod still gets its uplinks —
   but staging writes prod's real device data into the staging DB and evaluates
   alerts on it.
3. **Duplicate outbound notifications.** Following from (2), staging alert rows
   (very likely if the staging DB came from a prod dump) fire real notifications.
   Mailpit contains the email side; Twilio does not go through mailpit, so keep
   `TWILIO_*` empty.

**Only safe way to exercise MQTT ingest on staging:** point `MQTT_HOST` at a
dedicated staging broker — a `mosquitto` container added to this compose project,
or a separate ChirpStack *application* whose devices are staging-only — never at
the prod broker's topic tree. Then:

```bash
docker compose --env-file .env.staging -f docker-compose.staging.yml \
  --profile mqtt up -d agri-api-mqtt-staging
```

Never run more than one replica: every subscriber gets every message.

---

## 7. Redeploying staging

There is no CI job for this yet (gate 0.5). By hand:

```bash
cd /root/agri-api-staging && git pull
cd deploy/staging
docker compose --env-file .env.staging -f docker-compose.staging.yml \
  run --rm agri-api-web-staging bash /code/docker-entrypoint.sh migrate
docker compose --env-file .env.staging -f docker-compose.staging.yml up -d --build
```

Note the prod-side gotcha that applies here too: `docker compose up -d` does not
always recreate the MQTT container — if it is in use, add
`--force-recreate agri-api-mqtt-staging`.

---

## 8. Teardown

```bash
cd /root/agri-api-staging/deploy/staging

# Stop, keep the data:
docker compose --env-file .env.staging -f docker-compose.staging.yml --profile mqtt down

# Destroy everything staging, including the database:
docker compose --env-file .env.staging -f docker-compose.staging.yml --profile mqtt down -v
docker volume rm agri-api-staging_pgdata agri-api-staging_shared_data   # if any survive
docker image rm agri-api:staging
docker network rm agro-staging

# nginx
sudo rm /etc/nginx/sites-enabled/staging
sudo nginx -t && sudo systemctl reload nginx
```

`down -v` here removes **only** `agri-api-staging_*` volumes — prod's Postgres
volume belongs to the `agrilogy-back` compose project and is not referenced by
this file. Run these commands from `/root/agri-api-staging`, never from
`/root/agri-api`.

---

## 9. Collision table — prod vs staging

Every shared-namespace resource, with the prod value it must not collide with.

| Resource | Production | Staging |
|---|---|---|
| Compose project | `agri-api` (dir-derived) | `agri-api-staging` (explicit `name:`) |
| Compose file | `docker-compose.yml` (root, untouched) | `deploy/staging/docker-compose.staging.yml` |
| Checkout dir | `/root/agri-api` | `/root/agri-api-staging` |
| Env file | `back/.env` | `deploy/staging/.env.staging` |
| Image tag | `agri-api:latest` | `agri-api:staging` |
| Network | `agrilogy-back_agro` (external) | `agro-staging` (own, non-external) |
| Django container | `agri-api-web` | `agri-api-web-staging` |
| FastAPI container | `agri-api-fast` | `agri-api-fast-staging` |
| Celery worker | `agri-api-worker` | `agri-api-worker-staging` |
| Celery beat | `agri-api-beat` | `agri-api-beat-staging` |
| MQTT subscriber | `agri-api-mqtt` | `agri-api-mqtt-staging` (profile `mqtt`, off) |
| Postgres container | `agrydata` | `agrydata-staging` |
| Redis container | `redis` | `redis-staging` |
| Mailpit container | `mailpit` | `mailpit-staging` |
| Django host port | `8000` | `127.0.0.1:9000` |
| FastAPI host port | `8001` | `127.0.0.1:9001` |
| Postgres host port | `5432` | `127.0.0.1:9432` |
| Redis host port | `6379` | `127.0.0.1:9379` |
| Mailpit SMTP / UI | `1025` / `8025` | `127.0.0.1:9026` / `127.0.0.1:9025` |
| Postgres volume | `agrilogy-back` project volume | `agri-api-staging_pgdata` |
| `/shared` mount | bind `./shared_data` | volume `agri-api-staging_shared_data` |
| Database name | `agrilogy` | `agrilogy_staging` |
| Database user | `agrilogy` | `agrilogy_staging` |
| Celery broker | `redis://redis:6379/0` | `redis://redis-staging:6379/0` |
| Celery queue | `agriapi` (on prod redis) | `agriapi` (on **staging** redis — different broker) |
| Hostname | `back.agrogo-datafarm.com` | `staging.agrogo-datafarm.com` |
| nginx site | `deploy/nginx/back.conf` | `deploy/nginx/staging.conf` |
| TLS cert | `/etc/letsencrypt/live/back.agrogo-datafarm.com/` | `/etc/letsencrypt/live/staging.agrogo-datafarm.com/` |
| MQTT client id | `agri-api-ingest` | `agri-api-ingest-staging` |
| MQTT topic prefix | `agrilogy` | `agrilogy-staging` |
| `SECRET_KEY` | prod value | freshly generated, different |
| `RUN_DB_MIGRATIONS` | `false` (gated) | `true` |
| Email transport | Resend HTTP API (real) | SMTP → `mailpit-staging` (sink) |

Ports already in use on the droplet and deliberately avoided: `8000`, `8001`,
`8025`, `8080` (Adminer), `8081` (ChirpStack), `9090` (agri-bridge), `1025`,
`1883`, `3300` (Grafana), `5432`, `6379`.

Not shared with prod at all: the `agri-bridge` and `mosquitto` containers are
absent from the staging stack (bridge is not needed for a preprod API; mosquitto
would only be added deliberately for §6).
