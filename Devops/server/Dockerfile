FROM node:18-slim

WORKDIR /app

COPY server.js /app/server.js

# Create the shared directory and set permissions
RUN mkdir -p /shared && chmod 777 /shared && \
    touch /shared/requests.json && chmod 777 /shared/requests.json

EXPOSE 3000

CMD ["node", "server.js"]
