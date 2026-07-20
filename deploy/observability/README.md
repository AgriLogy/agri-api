# Log observability — Loki + Promtail + Grafana

Centralized, searchable logs for the whole droplet backend, self-hosted next to
the app. Nothing leaves the box.

```
 all containers ──stdout──┐
                          ├─▶ Promtail ──push──▶ Loki ──query──▶ Grafana ──▶ nginx TLS ──▶ you
 host /var/log/nginx ─────┘   (collector)      (store 30d)      (dashboards        (grafana.
                                                                 + alerts)          agrogo-datafarm.com)
```

* **Promtail** discovers every container via the Docker socket and tails the
  host nginx logs; ships both to Loki.
* **Loki** stores + indexes them (filesystem, ~30-day retention).
* **Grafana** is the UI: pre-provisioned Loki datasource, two dashboards, and a
  Resend-429 alert rule. Bound to `127.0.0.1:3300`; public only via nginx.

The app already emits **one JSON object per log line** (`fastapp.logging_config`)
with stable keys — `ts, level, logger, msg, request_id`, plus structured extras
(`event`, `status_code`, `container`, …). That's what makes the queries below
work. `LOG_FORMAT=text` in `back/.env` switches back to human-readable lines for
local dev.

---

## First-time bring-up (on the droplet)

1. **DNS** — add an A record `grafana.agrogo-datafarm.com → <droplet IP>`.

2. **Env** — from `deploy/observability/`:
   ```bash
   cp .env.observability.example .env
   # edit .env → set a strong GF_SECURITY_ADMIN_PASSWORD (Postgres/webhook optional)
   ```

3. **Start the stack** (own compose file; unaffected by backend deploys):
   ```bash
   cd deploy/observability
   docker compose -f docker-compose.observability.yml up -d
   docker compose -f docker-compose.observability.yml ps
   ```

4. **nginx + TLS** (host nginx, same pattern as `back.conf`):
   ```bash
   sudo cp ../nginx/grafana.conf /etc/nginx/sites-available/grafana
   sudo ln -sf /etc/nginx/sites-available/grafana /etc/nginx/sites-enabled/grafana
   sudo certbot --nginx -d grafana.agrogo-datafarm.com
   sudo nginx -t && sudo systemctl reload nginx
   ```

5. **Log in** at `https://grafana.agrogo-datafarm.com` (admin + your password).
   Dashboards are under **Dashboards → Agrilogy**.

---

## What you get

**Dashboards** (auto-provisioned, folder *Agrilogy*):
- **Agri — Logs Overview** — lines/min by level & container, error/warning
  counters, live error+warning stream. Filter by container.
- **Agri — Notifications & Email** — emails sent/failed/skipped, **Resend 429
  quota hits**, outcomes over time, live failed-send stream.

**Handy LogQL** (Explore → Loki):
```logql
# everything from the FastAPI sidecar
{container="agri-api-fast"}

# errors across all containers
{job="docker", level="ERROR"}

# one request end-to-end (grab the id from any line or the X-Request-ID header)
{job="docker"} | json | request_id="e3b0c44298fc1c14"

# every failed email send, newest first
{job="docker"} | json | event=~`notify.email.(failed|error)`

# Resend quota (429) events only
{job="docker"} | json | event=`notify.email.failed` | quota_exceeded=`true`

# slow requests (>1s)
{container="agri-api-fast"} | json | event=`http.access` | duration_ms > 1000

# nginx 5xx
{job="nginx"} |= " 50" | logfmt
```

---

## Alerting (Resend-429)

The rule `resend-quota-429` is provisioned and evaluates every minute; it shows
in **Alerting → Alert rules** and turns the dashboard tile red the moment a 429
appears. Delivery is wired to **Discord** (SMTP is blocked on the droplet):

- Contact point `Agri Discord` + a route sending `severity=critical` there are
  provisioned in `grafana/provisioning/alerting/contactpoints.yml`.
- The Discord channel-webhook URL is the only secret: set `GF_ALERT_WEBHOOK_URL`
  in `deploy/observability/.env` (the compose file forwards it into Grafana), then
  reprovision:
  ```bash
  docker compose -f docker-compose.observability.yml up -d grafana
  ```
- Test it: **Alerting → Contact points → Agri Discord → Test**, or wait for a real
  429. To switch channels later, change the contact-point `type` (`slack`,
  `webhook`, …) in that file — the URL stays in `.env`.

---

## Operations

- **Update after config edits:** `docker compose -f docker-compose.observability.yml up -d`
  (Grafana reprovisions datasources/dashboards/alerts on restart).
- **Disk:** Loki retention is 30d (`loki/loki-config.yml → retention_period`).
  App-container logs are also capped (`logging:` block in the app compose:
  10MB × 5 files each) so the raw json-file logs can't fill the disk either.
- **Loki won't start (permission denied on /loki):** one-time fix —
  `docker compose -f docker-compose.observability.yml run --rm --user root loki chown -R 10001:10001 /loki`
  then `up -d`.
- **Check Promtail is shipping:** `curl -s localhost:9080/metrics | grep promtail_sent_entries` (inside the droplet).
- **Security:** Grafana binds `127.0.0.1` only (droplet has no firewall). Never
  change it to `0.0.0.0` / publish it — go through nginx TLS.

## Related

- Log shape / knobs: `back/src/fastapp/logging_config.py`, `LOG_LEVEL` /
  `LOG_FORMAT` in `back/.env`.
- Structured event names live at the call sites: `http.access` / `http.error`
  (`fastapp/middleware.py`), `notify.email.*` (`fastapp/email.py`), `mqtt.*`
  (`fastapp/mqtt.py`).
