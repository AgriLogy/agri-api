# syntax=docker/dockerfile:1
# agri-bridge — HTTP ingest gateway. Multi-stage so the runtime image
# doesn't carry build tooling.

FROM node:20-slim AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci --omit=dev; else npm install --omit=dev; fi

FROM node:20-slim AS runtime
ENV NODE_ENV=production \
    PORT=9090
WORKDIR /app
COPY --from=deps /app/node_modules /app/node_modules
COPY package.json ./
COPY src ./src
EXPOSE 9090
USER node
CMD ["node", "src/server.js"]
