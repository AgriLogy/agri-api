FROM node:18-slim

WORKDIR /app

COPY server.js /app/server.js

# The archive lives on the ./shared_data bind mount at /shared_data, not here.
# This /shared scaffolding predates that and is inert — kept only so the image
# stays byte-for-byte comparable with the one currently running on the droplet.
RUN mkdir -p /shared && chmod 777 /shared && \
    touch /shared/requests.json && chmod 777 /shared/requests.json

# server.js binds 9090 (it always did — the 3000 here was wrong metadata).
EXPOSE 9090

CMD ["node", "server.js"]
