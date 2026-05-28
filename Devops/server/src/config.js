/**
 * Runtime configuration for agri-bridge.
 *
 * All values come from environment variables with safe dev defaults.
 * Reads happen ONCE at module load — never re-read mid-request.
 */
import process from "node:process";

export const config = Object.freeze({
  // HTTP binding
  bindHost: process.env.BIND_HOST ?? "0.0.0.0",
  port: Number(process.env.PORT ?? 9090),

  // Backend (agri-api) — where we forward translated payloads
  backendUrl: process.env.BACKEND_URL ?? "http://agri-api-web:8000",
  backendPath: process.env.BACKEND_PATH ?? "/api/v1/bivocom/uplink",
  backendTimeoutMs: Number(process.env.BACKEND_TIMEOUT_MS ?? 8000),

  // Retry policy on transient backend failures
  retryAttempts: Number(process.env.RETRY_ATTEMPTS ?? 3),
  retryBackoffMs: (process.env.RETRY_BACKOFF_MS ?? "100,500,2000")
    .split(",")
    .map((s) => Number(s.trim()))
    .filter((n) => Number.isFinite(n) && n >= 0),

  // Logging
  logLevel: process.env.LOG_LEVEL ?? "info",

  // Optional shared-secret auth (TODO: enforce when set; follow-up PR)
  sharedSecret: process.env.AGRI_BRIDGE_SHARED_SECRET ?? null,
});
