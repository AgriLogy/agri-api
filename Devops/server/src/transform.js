/**
 * Translate a Router0X-format payload into the Bivocom v1 wire format
 * accepted by agri-api's POST /api/v1/bivocom/uplink endpoint.
 *
 * Router0X → Bivocom v1 mapping:
 *   user         → device_id = `router-user-${user}`
 *   timestamp    → timestamp (passed through; backend parses)
 *   <sensor_key> → tags[sensor_key] (every numeric field becomes a tag)
 *
 * Pure function — no I/O, no logging, no state. Tested in isolation.
 */

const RESERVED_KEYS = new Set(["user", "timestamp"]);

/**
 * @param {import("./schema.js").Router0XPayload} payload
 * @returns {{device_id: string, timestamp: string, tags: Record<string, number>}}
 */
export function toBivocomUplink(payload) {
  const tags = {};
  for (const [k, v] of Object.entries(payload)) {
    if (RESERVED_KEYS.has(k)) continue;
    if (typeof v !== "number" || !Number.isFinite(v)) continue;
    tags[k] = v;
  }

  return {
    device_id: `router-user-${payload.user}`,
    timestamp: payload.timestamp ?? new Date().toISOString(),
    tags,
  };
}
