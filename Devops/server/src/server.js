/**
 * agri-bridge — HTTP ingest gateway.
 *
 * Translates Router0X-format payloads from field devices into the
 * Bivocom v1 wire format and forwards to agri-api.
 *
 * Endpoints:
 *   POST /          — legacy route (what devices hit today)
 *   POST /uplink    — cleaner alias for /
 *   GET  /health    — liveness probe (always 200 if process is up)
 *   GET  /ready     — readiness probe (also pings backend)
 *
 * Stateless: no file persistence. Postgres (via agri-api) is the source
 * of truth. The previous flat-file `requests.json` was a bootstrap hack
 * and is now gone.
 */
import express from "express";
import { pinoHttp } from "pino-http";
import pino from "pino";

import { config } from "./config.js";
import { router0xPayload } from "./schema.js";
import { toBivocomUplink } from "./transform.js";
import { forwardToBackend } from "./forward.js";

const log = pino({ level: config.logLevel });

export function createApp() {
  const app = express();
  app.use(express.json({ limit: "256kb" }));
  app.use(pinoHttp({ logger: log }));

  app.get("/health", (_req, res) => {
    res.status(200).json({ status: "ok" });
  });

  app.get("/ready", async (_req, res) => {
    try {
      const r = await fetch(`${config.backendUrl}/admin/login/`, {
        signal: AbortSignal.timeout(2000),
      });
      const backendOk = r.status < 500;
      res.status(backendOk ? 200 : 503).json({
        status: backendOk ? "ready" : "backend_unreachable",
        backend_status: r.status,
      });
    } catch (err) {
      res.status(503).json({
        status: "backend_unreachable",
        error: err?.message ?? String(err),
      });
    }
  });

  const ingestHandler = async (req, res) => {
    const parsed = router0xPayload.safeParse(req.body);
    if (!parsed.success) {
      req.log.warn({ errors: parsed.error.errors }, "router0x.invalid");
      return res.status(400).json({
        error: {
          code: "validation_error",
          message: "Invalid Router0X payload",
          details: parsed.error.errors,
        },
      });
    }

    const bivocom = toBivocomUplink(parsed.data);
    req.log.info(
      { device_id: bivocom.device_id, tag_count: Object.keys(bivocom.tags).length },
      "router0x.accepted"
    );

    const result = await forwardToBackend(bivocom);
    if (result.ok) {
      req.log.info(
        { status: result.status, attempts: result.attempts },
        "forward.ok"
      );
      return res.status(202).json({
        accepted: true,
        device_id: bivocom.device_id,
        tag_count: Object.keys(bivocom.tags).length,
        backend_status: result.status,
        attempts: result.attempts,
      });
    }

    req.log.error(
      { status: result.status, attempts: result.attempts, body: result.body },
      "forward.failed"
    );
    // Permanent backend errors (we sent a bad payload) → 502 so it's visible
    // Transient errors after retries exhausted → also 502
    return res.status(502).json({
      error: {
        code: "backend_unavailable",
        message: "Backend rejected or did not respond after retries.",
        backend_status: result.status,
        backend_body: result.body?.slice(0, 500),
        attempts: result.attempts,
      },
    });
  };

  app.post("/", ingestHandler);
  app.post("/uplink", ingestHandler);

  return app;
}

// Auto-start unless we're being imported by tests
if (import.meta.url === `file://${process.argv[1]}`) {
  const app = createApp();
  app.listen(config.port, config.bindHost, () => {
    log.info(
      { port: config.port, host: config.bindHost, backend: `${config.backendUrl}${config.backendPath}` },
      "agri-bridge listening"
    );
  });
}
