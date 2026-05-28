/**
 * HTTP forwarder for translated Bivocom payloads.
 *
 * Behaviour:
 *   * Single POST attempt has an explicit timeout (BACKEND_TIMEOUT_MS).
 *   * On transient failure (network error OR 5xx OR 408/429), retry with
 *     the configured backoff schedule.
 *   * On 4xx (other than 408/429), do NOT retry — that's a permanent
 *     client error (e.g. our transform produced an invalid payload).
 *   * On success, return {status, body} for the caller to surface.
 *
 * Uses the built-in global `fetch` available in Node 18+.
 */
import { config } from "./config.js";

const TRANSIENT_STATUSES = new Set([408, 429, 500, 502, 503, 504]);

function isTransientStatus(status) {
  return TRANSIENT_STATUSES.has(status);
}

async function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * One POST attempt with timeout.
 * @returns {Promise<{ok: boolean, status: number, body: string, transient: boolean}>}
 */
async function attempt(url, payload, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    const body = await res.text();
    return {
      ok: res.ok,
      status: res.status,
      body,
      transient: isTransientStatus(res.status),
    };
  } catch (err) {
    // AbortError / network errors are transient
    return {
      ok: false,
      status: 0,
      body: err?.message ?? String(err),
      transient: true,
    };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Forward a translated payload to agri-api with retry on transient failures.
 *
 * @param {object} bivocomPayload
 * @param {object} [opts]
 * @param {string} [opts.url] override config (mainly for tests)
 * @param {number[]} [opts.backoff] override config (mainly for tests)
 * @param {number} [opts.timeoutMs] override config
 * @returns {Promise<{ok: boolean, status: number, body: string, attempts: number}>}
 */
export async function forwardToBackend(bivocomPayload, opts = {}) {
  const url =
    opts.url ?? `${config.backendUrl}${config.backendPath}`;
  const backoff =
    opts.backoff ?? config.retryBackoffMs.slice(0, config.retryAttempts - 1);
  const timeoutMs = opts.timeoutMs ?? config.backendTimeoutMs;

  let attemptCount = 0;
  let last = null;

  // First attempt + up to backoff.length retries
  for (let i = 0; i <= backoff.length; i++) {
    attemptCount++;
    last = await attempt(url, bivocomPayload, timeoutMs);
    if (last.ok) {
      return { ...last, attempts: attemptCount };
    }
    if (!last.transient) {
      // Permanent failure (4xx other than 408/429) — don't retry
      return { ...last, attempts: attemptCount };
    }
    if (i < backoff.length) {
      await sleep(backoff[i]);
    }
  }
  return { ...last, attempts: attemptCount };
}
