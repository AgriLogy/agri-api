const http = require('http');
const fs = require('fs');
const path = require('path');

const sharedDir = '/shared_data';
const jsonFilePath = path.join(sharedDir, 'requests.json');
const logFilePath = path.join(__dirname, '.logs');

const PY_HOST = process.env.PY_HOST || "agrybackend";
const PY_PORT = Number(process.env.PY_PORT || 8000);
const PY_PATH = process.env.PY_PATH || "/api/sensors/weather/ingest/";

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
        timeout: 8000,
      },
      (res) => {
        let body = "";
        res.on("data", (c) => (body += c.toString()));
        res.on("end", () => {
          const ok = res.statusCode >= 200 && res.statusCode < 300;
          if (!ok) return reject(new Error(`Python ${res.statusCode}: ${body}`));
          resolve(body);
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

    req.on('data', chunk => {
        body += chunk.toString();
    });

    req.on('end', async () => {
        const timestamp = new Date().toISOString();
        let logEntry = `${timestamp} | `;

        let newData;
        try {
            newData = JSON.parse(body);
        } catch (error) {
            // Invalid JSON
            logEntry += `INVALID | ${body}\n`;
            fs.appendFileSync(logFilePath, logEntry);
            res.writeHead(400, { 'Content-Type': 'text/plain' });
            res.end('Invalid JSON');
            return;
        }

        // Valid JSON
        let existingData = [];

        if (fs.existsSync(jsonFilePath)) {
            try {
                const rawData = fs.readFileSync(jsonFilePath);
                existingData = JSON.parse(rawData);
            } catch (error) {
                // If JSON is malformed, start fresh
                existingData = [];
            }
        }

        existingData.push(newData);
        fs.writeFileSync(jsonFilePath, JSON.stringify(existingData, null, 2));

        // only forward if it is a JSON object (not array)
        if (newData && typeof newData === "object" && !Array.isArray(newData)) {
        try {
            await postToPython(newData);
            logEntry += ` | FORWARDED_OK`;
            console.log("true")
        } catch (e) {
            logEntry += ` | FORWARDED_FAIL: ${e.message}`;
            console.log("false")
        }
        }


        logEntry += `VALID | ${JSON.stringify(newData)}\n`;
        fs.appendFileSync(logFilePath, logEntry);

        res.writeHead(200, { 'Content-Type': 'text/plain' });
        res.end('Data Received Successfully !!');
    });
});

const PORT = 9090;
const HOST = "0.0.0.0";
server.listen(PORT, HOST);
