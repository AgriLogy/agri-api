# Single small droplet (512 MB RAM + 2 GB swap)

Host details (IP, login, host-key fingerprint) are kept out of the repo.

- Compose files live in the deploy user's `~/agri-api`: `docker-compose.yml`,
  this override, `.env` (compose vars `POSTGRES_PASSWORD`, `CHIRPSTACK_NETWORK`)
  and `back/.env` (app settings, `DJANGO_ENV=prod`).
- Postgres is the `agrydata` container (named volume `pgdata`); schema is
  brought to head with the entrypoint `migrate` role.
- Docker must use the classic overlay2 store (`/etc/docker/daemon.json`:
  `{"features":{"containerd-snapshotter":false}}`). The containerd store keeps
  two copies of each image and fills a 10 GB disk.
- The box is too small to build images: build `linux/amd64` on a laptop,
  `docker save | gzip | ssh … 'gunzip | docker load'`, run `migrate`, then
  `up -d` the app services.
- Public: nginx 80/443 (`deploy/nginx/back.conf`) and the Bivocom bridge 9090.
  Redis, mosquitto, Postgres, :8000 and :8001 bind to 127.0.0.1 (Docker
  publishes bypass ufw).
- `deploy-back.yml` stays disabled: it builds on the host, which this box
  cannot do.
