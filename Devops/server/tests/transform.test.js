import { describe, expect, it } from "vitest";

import { router0xPayload } from "../src/schema.js";
import { toBivocomUplink } from "../src/transform.js";

describe("Router0X → Bivocom transform", () => {
  it("maps the full curl-example payload", () => {
    const parsed = router0xPayload.parse({
      user: "user1",
      precipitation_rate: 15,
      humidity_weather: 70.0,
      wind_speed: 35.42,
      solar_radiation: 420.25,
      pressure_weather: 900.09,
      timestamp: "2025-03-15 00:02:08.066",
    });
    const out = toBivocomUplink(parsed);
    expect(out.device_id).toBe("router-user-user1");
    expect(out.timestamp).toBe("2025-03-15 00:02:08.066");
    expect(out.tags.precipitation_rate).toBe(15);
    expect(out.tags.humidity_weather).toBe(70.0);
    expect(out.tags.wind_speed).toBe(35.42);
    // `user` + `timestamp` must NOT appear in tags
    expect(out.tags.user).toBeUndefined();
    expect(out.tags.timestamp).toBeUndefined();
  });

  it("coerces numeric user to string", () => {
    const parsed = router0xPayload.parse({ user: 42, temp: 23 });
    const out = toBivocomUplink(parsed);
    expect(out.device_id).toBe("router-user-42");
  });

  it("falls back to current ISO when timestamp absent", () => {
    const parsed = router0xPayload.parse({ user: "u", temp: 23 });
    const out = toBivocomUplink(parsed);
    expect(out.timestamp).toMatch(/^\d{4}-\d{2}-\d{2}T/);
  });

  it("drops non-finite numbers", () => {
    // zod rejects non-finite numbers, but if we ever bypass schema...
    const out = toBivocomUplink({ user: "u", temp: Number.POSITIVE_INFINITY, ok: 5 });
    expect(out.tags.temp).toBeUndefined();
    expect(out.tags.ok).toBe(5);
  });
});

describe("Router0X zod schema", () => {
  it("rejects empty user", () => {
    expect(router0xPayload.safeParse({ user: "" }).success).toBe(false);
  });

  it("rejects non-numeric sensor values", () => {
    const r = router0xPayload.safeParse({ user: "u", temp: "hot" });
    expect(r.success).toBe(false);
  });

  it("allows arbitrary extra sensor keys", () => {
    const r = router0xPayload.safeParse({
      user: "u",
      brand_new_sensor: 99.9,
    });
    expect(r.success).toBe(true);
    expect(r.data.brand_new_sensor).toBe(99.9);
  });
});
