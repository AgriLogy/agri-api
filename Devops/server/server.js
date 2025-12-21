const http = require('http');
const fs = require('fs');
const path = require('path');

// Use the shared volume path
const sharedDir = '/shared';
const jsonFilePath = path.join(sharedDir, 'requests.json');
const logFilePath = path.join(__dirname, '.logs');

const server = http.createServer((req, res) => {
    let body = '';

    req.on('data', chunk => {
        body += chunk.toString();
    });

    req.on('end', () => {
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

        logEntry += `VALID | ${JSON.stringify(newData)}\n`;
        fs.appendFileSync(logFilePath, logEntry);

        res.writeHead(200, { 'Content-Type': 'text/plain' });
        res.end('Valid JSON saved.');
    });
});

const PORT = 9090;
const HOST = "0.0.0.0"; // Accept connections from other containers and outside
server.listen(PORT, HOST);
