#!/bin/sh
# Health beacon — emits JSON lines to stdout every 60s so Promtail ships them to
# Loki and Grafana can alert on things pure log-scraping can't see: host disk
# fill and container up/down. One tiny container instead of a full Prometheus +
# node-exporter + cadvisor stack.
#
# Reads host / (bind-mounted read-only at /host) for disk, and the Docker socket
# (read-only) for container states. Field name is "name" (not "container") to
# avoid colliding with Promtail's own `container` label on `| json`.
set -eu

while true; do
  # Host root filesystem usage as an integer percent.
  df -P /host 2>/dev/null | awk 'NR==2 {gsub("%","",$5); printf "{\"event\":\"host.disk\",\"mount\":\"/\",\"use_percent\":%d}\n", $5}'

  # One line per container: its name + state (running|exited|restarting|…). An
  # exited/restarting agri-api-* container therefore surfaces as state!=running.
  docker ps -a --format '{{.Names}}	{{.State}}' 2>/dev/null | while IFS='	' read -r name state; do
    printf '{"event":"container.state","name":"%s","state":"%s"}\n' "$name" "$state"
  done

  sleep 60
done
