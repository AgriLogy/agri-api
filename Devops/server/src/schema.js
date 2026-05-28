/**
 * Zod schema for the Router0X HTTP-push payload.
 *
 * Wire shape (legacy — device firmware can't change quickly):
 *
 *   {
 *     "user": 2 | "user1",
 *     "timestamp": "2025-03-15 00:02:08.066",
 *     "humidity_weather": 70.0,
 *     "wind_speed": 35.42,
 *     "soil_moisture_low": 55.0,
 *     ... (any number of sensor_key: float pairs)
 *   }
 *
 * We model:
 *   - `user` as either string or number (devices vary; we coerce to string)
 *   - `timestamp` as string (parsed by the backend, not us)
 *   - any other key as a finite number (sensor reading)
 *
 * Extra unknown keys are passed through — devices may add new sensor
 * types and we don't want to 400 on them.
 */
import { z } from "zod";

const finiteNumber = z.number().finite();

export const router0xPayload = z
  .object({
    user: z.union([z.string().min(1), z.number()]).transform(String),
    timestamp: z.string().min(1).optional(),
  })
  .catchall(finiteNumber);

/**
 * @typedef {z.infer<typeof router0xPayload>} Router0XPayload
 */
