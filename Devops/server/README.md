# agri-bridge

HTTP ingest gateway. Translates Router0X-format payloads from field
devices into the Bivocom v1 wire format and forwards to `agri-api`.

```
device → POST :9090/    →  agri-bridge  →  POST :8000/api/v1/bivocom/uplink
                            (translate)      (pydantic-validated)
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/` | Legacy device route (Router0X JSON in) |
| POST | `/uplink` | Cleaner alias for `/` |
| GET | `/health` | Liveness — 200 if the process is up |
| GET | `/ready` | Readiness — 200 if backend `/admin/login/` is reachable |

## Wire format — what devices send

```json
{
  "user": "user1",
  "timestamp": "2025-03-15 00:02:08.066",
  "humidity_weather": 70.0,
  "wind_speed": 35.42,
  "soil_moisture_low": 55.0
}
```

- `user`: required; string or number (coerced to string)
- `timestamp`: optional; if omitted the bridge stamps `now()` ISO
- Any other key: required to be a finite number (sensor reading)

The bridge tolerates new sensor keys without code changes.

## Wire format — what the bridge sends to `agri-api`

```json
{
  "device_id": "router-user-user1",
  "timestamp": "2025-03-15 00:02:08.066",
  "tags": {
    "humidity_weather": 70.0,
    "wind_speed": 35.42,
    "soil_moisture_low": 55.0
  }
}
```

Validated by `agri-api`'s pydantic `BivocomUplink` schema.

## Status codes

| Code | When |
|---|---|
| 202 | Valid Router0X payload, backend forwarded successfully (after up to 3 attempts) |
| 400 | Invalid Router0X payload (zod-rejected) |
| 502 | Backend returned 4xx (bug) OR all retry attempts failed (transient) |

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `PORT` | `9090` | HTTP bind port |
| `BIND_HOST` | `0.0.0.0` | HTTP bind host |
| `BACKEND_URL` | `http://agri-api-web:8000` | agri-api base URL |
| `BACKEND_PATH` | `/api/v1/bivocom/uplink` | agri-api ingest path |
| `BACKEND_TIMEOUT_MS` | `8000` | per-attempt timeout |
| `RETRY_ATTEMPTS` | `3` | total attempts (1 + 2 retries) |
| `RETRY_BACKOFF_MS` | `100,500,2000` | backoff schedule (ms between retries) |
| `LOG_LEVEL` | `info` | pino log level |

## Tests

```bash
cd Devops/server
npm install
npm test           # vitest, runs unit + integration tests
npm run test:watch
```

Unit tests cover the schema + transform (pure logic). Integration tests
exercise the Express app with a stubbed backend via `vi.spyOn` on `fetch`.

## Notes

- **No file persistence** — the legacy `shared_data/requests.json` was a
  bootstrap hack for when the Django side wasn't ready. Now Postgres (via
  `agri-api`) is the source of truth and the bridge is stateless.
- **No shared-secret auth** yet — a `X-Agri-Token` header check is queued
  for a follow-up PR (will be enforced on both bridge and the
  `/api/v1/bivocom/uplink` endpoint together).
