const http = require('http');
const fs = require('fs');
const path = require('path');

const sharedDir = process.env.SHARED_DIR || '/shared_data';
// Append-only JSONL. The previous requests.json was a single JSON *array*
// that was read, parsed, appended to and re-serialized on EVERY uplink —
// O(file) memory and disk per request. At 52 MB that peaked ~1.1 GB RSS and
// got the process OOM-killed by the kernel every ~70 minutes.
const jsonlFilePath = path.join(sharedDir, 'requests.jsonl');
const logFilePath = path.join(__dirname, '.logs');

const PY_HOST = process.env.PY_HOST || "agrybackend";
const PY_PORT = Number(process.env.PY_PORT || 8000);
const PY_PATH = process.env.PY_PATH || "/api/sensors/weather/ingest/";
// Gunicorn upstream runs --timeout 60; giving up at 8 s discarded uplinks the
// backend went on to persist anyway. 20 s keeps a slow-but-alive backend from
// costing us data while still shedding a genuinely hung one.
const PY_TIMEOUT_MS = Number(process.env.PY_TIMEOUT_MS || 20000);
// Port 9090 is internet-reachable; cap the in-memory body so a broken or
// hostile device can't grow it without bound.
const MAX_BODY_BYTES = Number(process.env.MAX_BODY_BYTES || 65536);

// Fire-and-forget append. Never blocks the event loop, never fails a request.
function appendLine(file, line) {
  fs.appendFile(file, line, (err) => {
    if (err) console.error(`[archive] append failed file=${file} error=${err.message}`);
  });
}

function postToPython(payload) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(payload);

    const req = http.request(
      {
        hostname: PY_HOST,
        port: PY_PORT,
        path: PY_PATH,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(data),
        },
        timeout: PY_TIMEOUT_MS,
      },
      (res) => {
        let body = "";
        res.on("data", (c) => (body += c.toString()));
        res.on("end", () => {
          const ok = res.statusCode >= 200 && res.statusCode < 300;
          if (!ok) return reject(new Error(`Python ${res.statusCode}: ${body}`));
          resolve({ statusCode: res.statusCode, body });
        });
      }
    );

    req.on("timeout", () => req.destroy(new Error("Python timeout")));
    req.on("error", reject);
    req.write(data);
    req.end();
  });
}

const server = http.createServer((req, res) => {
    let body = '';
    let tooLarge = false;

    req.on('data', chunk => {
        if (tooLarge) return;
        body += chunk.toString();
        if (Buffer.byteLength(body) > MAX_BODY_BYTES) {
            tooLarge = true;
            body = '';
            res.writeHead(413, { 'Content-Type': 'text/plain' });
            res.end('Payload too large');
            req.destroy();
        }
    });

    req.on('end', async () => {
        if (tooLarge) return;

        const timestamp = new Date().toISOString();
        let logEntry = `${timestamp} | `;

        let newData;
        try {
            newData = JSON.parse(body);
        } catch (error) {
            logEntry += `INVALID | ${body}\n`;
            appendLine(logFilePath, logEntry);
            res.writeHead(400, { 'Content-Type': 'text/plain' });
            res.end('Invalid JSON');
            return;
        }

        // One line per uplink — constant memory, constant disk, no re-parse.
        appendLine(jsonlFilePath, JSON.stringify(newData) + '\n');

        // only forward if it is a JSON object (not array)
        if (newData && typeof newData === "object" && !Array.isArray(newData)) {
            const client = newData.client || "<unknown>";
            const keyCount = Object.keys(newData).filter((k) => k !== "client").length;
            try {
                const { statusCode, body: respBody } = await postToPython(newData);
                logEntry += ` | FORWARDED_OK`;
                console.log(
                    `[${timestamp}] forward ok   client=${client} keys=${keyCount} status=${statusCode} body=${respBody}`
                );
            } catch (e) {
                logEntry += ` | FORWARDED_FAIL: ${e.message}`;
                console.error(
                    `[${timestamp}] forward FAIL client=${client} keys=${keyCount} error=${e.message}`
                );
            }
        }

        logEntry += `VALID | ${JSON.stringify(newData)}\n`;
        appendLine(logFilePath, logEntry);

        res.writeHead(200, { 'Content-Type': 'text/plain' });
        res.end('Data Received Successfully !!');
    });
});

const PORT = 9090;
const HOST = "0.0.0.0";
server.listen(PORT, HOST);
