# SSO / CORS origins — production checklist

The agri-identity SSO gateway routes one login into the farmer web app and the
admin console (see the `agri-identity` repo). For that to work in production,
agri-api's CORS/CSRF origin lists must include every **browser** origin that
calls this API.

## What actually needs CORS

CORS is enforced by the browser, so only origins that make **browser-side**
requests to agri-api need an entry:

| Origin                              | Calls API from browser? | Needs CORS/CSRF |
| ----------------------------------- | ----------------------- | --------------- |
| `https://app.agrogo-datafarm.com`   | yes (farmer web axios)  | **yes**         |
| `https://admin.agrogo-datafarm.com` | yes (admin axios)       | **yes**         |
| `https://identity.agrogo-datafarm.com` | no — the gateway calls the API server-side (Next route handlers / server components) | defensive only |
| `https://www.agrogo-datafarm.com`   | marketing site          | already listed  |

The gateway never makes a browser-side call to agri-api, so strictly it does not
need an entry. We list it anyway so a future browser call from it would not be
blocked.

## Production env (droplet `.env`)

Prod reads both lists from the environment (comma-separated, no spaces). Set:

```env
CORS_ALLOWED_ORIGINS=https://app.agrogo-datafarm.com,https://admin.agrogo-datafarm.com,https://identity.agrogo-datafarm.com,https://www.agrogo-datafarm.com
CSRF_TRUSTED_ORIGINS=https://app.agrogo-datafarm.com,https://admin.agrogo-datafarm.com,https://identity.agrogo-datafarm.com,https://www.agrogo-datafarm.com
```

Restart the web container after editing so Django re-reads the env.

## Local dev

`settings/dev.py` already defaults to localhost `:3000` (web), `:3001` (admin),
and `:3002` (identity), so a locally-run app family works against a local API
with no env override.
