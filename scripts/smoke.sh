#!/usr/bin/env bash
#
# smoke.sh — per-phase HTTP smoke test for agri-api.
#
# Hits the public endpoints with curl, asserts status < 500, prints
# pass/fail. Exit 0 on all-pass, non-zero on first failure.
#
# Usage:
#   ./scripts/smoke.sh                       # defaults BASE_URL=http://localhost:8000
#   BASE_URL=http://localhost:8000 ./scripts/smoke.sh
#   BASE_URL=https://api.agrologyy.com ./scripts/smoke.sh
#
# Run after every phase merge against `make up` (or against the
# deployed host). Phase merges must keep all checks green.

set -uo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
TIMEOUT="${TIMEOUT:-5}"
WAIT_FOR_READY="${WAIT_FOR_READY:-true}"
READY_DEADLINE_S="${READY_DEADLINE_S:-90}"

GREEN='\033[1;32m'
RED='\033[1;31m'
DIM='\033[2m'
RESET='\033[0m'

pass=0
fail=0

# ---------------------------------------------------------------------------
# Wait for the server to start responding (useful right after `make up`).
# ---------------------------------------------------------------------------
wait_for_ready() {
    [[ "$WAIT_FOR_READY" != "true" ]] && return 0
    local deadline=$(( $(date +%s) + READY_DEADLINE_S ))
    while (( $(date +%s) < deadline )); do
        if curl -fsS -o /dev/null -m 2 "${BASE_URL}/admin/login/" 2>/dev/null; then
            return 0
        fi
        sleep 2
    done
    printf "${RED}server at %s did not become ready within %ss${RESET}\n" \
        "$BASE_URL" "$READY_DEADLINE_S" >&2
    return 1
}

# ---------------------------------------------------------------------------
# check <name> <method> <path> [content-type-substring]
#   asserts status < 500, optionally asserts content-type substring.
# ---------------------------------------------------------------------------
check() {
    local name="$1" method="$2" path="$3" want_ct="${4:-}"
    local url="${BASE_URL}${path}"
    local out status ct
    out=$(curl -sSL -o /dev/null -m "$TIMEOUT" \
        -w '%{http_code}|%{content_type}' \
        -X "$method" "$url" 2>&1) || {
        printf "${RED}✗${RESET} %-32s ${DIM}%s %s — curl failed: %s${RESET}\n" \
            "$name" "$method" "$path" "$out"
        fail=$((fail + 1))
        return
    }
    status="${out%%|*}"
    ct="${out#*|}"

    if [[ -z "$status" ]] || (( status >= 500 )); then
        printf "${RED}✗${RESET} %-32s ${DIM}%s %s — status=%s ct=%s${RESET}\n" \
            "$name" "$method" "$path" "$status" "$ct"
        fail=$((fail + 1))
        return
    fi
    if [[ -n "$want_ct" && "$ct" != *"$want_ct"* ]]; then
        printf "${RED}✗${RESET} %-32s ${DIM}%s %s — status=%s ct=%s want=%s${RESET}\n" \
            "$name" "$method" "$path" "$status" "$ct" "$want_ct"
        fail=$((fail + 1))
        return
    fi
    printf "${GREEN}✓${RESET} %-32s ${DIM}%s %s — status=%s${RESET}\n" \
        "$name" "$method" "$path" "$status"
    pass=$((pass + 1))
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
printf "smoke — %s\n" "$BASE_URL"
printf "%s\n" "-----------------------------------"

if ! wait_for_ready; then
    printf "${RED}FAIL${RESET} server never became ready\n"
    exit 2
fi

# --- Phase 0 smoke: public endpoints only ---
check "admin login page"   GET /admin/login/  text/html
check "swagger UI"         GET /swagger/      text/html
check "redoc UI"           GET /redoc/        text/html
check "openapi schema"     GET /swagger.json  application/json

# --- Per-domain checks (Phase 5+) ---

# Bivocom uplink (Phase 5a). POST minimal pydantic-valid body; expect 202.
bivocom_payload='{"device_id":"BV-smoke","timestamp":"2026-05-28T11:00:00Z","tags":{"ta":21.5}}'
bivocom_out=$(curl -sSL -m "$TIMEOUT" \
    -X POST -H "Content-Type: application/json" \
    -d "$bivocom_payload" \
    -w '%{http_code}' -o /tmp/smoke_bivocom_body \
    "${BASE_URL}/api/v1/bivocom/uplink" 2>&1)
if [[ "$bivocom_out" == "202" ]]; then
    printf "${GREEN}✓${RESET} %-32s ${DIM}POST /api/v1/bivocom/uplink — status=202${RESET}\n" "bivocom uplink (valid)"
    pass=$((pass + 1))
else
    printf "${RED}✗${RESET} %-32s ${DIM}POST /api/v1/bivocom/uplink — status=%s body=%s${RESET}\n" \
        "bivocom uplink (valid)" "$bivocom_out" "$(cat /tmp/smoke_bivocom_body 2>/dev/null | head -c 200)"
    fail=$((fail + 1))
fi

# Bivocom pydantic rejection — bad payload should return 400
bad_out=$(curl -sSL -m "$TIMEOUT" \
    -X POST -H "Content-Type: application/json" \
    -d '{"device_id":""}' \
    -w '%{http_code}' -o /dev/null \
    "${BASE_URL}/api/v1/bivocom/uplink" 2>&1)
if [[ "$bad_out" == "400" ]]; then
    printf "${GREEN}✓${RESET} %-32s ${DIM}POST /api/v1/bivocom/uplink — status=400 (pydantic-rejected)${RESET}\n" "bivocom uplink (invalid)"
    pass=$((pass + 1))
else
    printf "${RED}✗${RESET} %-32s ${DIM}POST /api/v1/bivocom/uplink — status=%s (expected 400)${RESET}\n" \
        "bivocom uplink (invalid)" "$bad_out"
    fail=$((fail + 1))
fi

# Bridge end-to-end (Phase 5x). POST a Router0X-format payload to :9090,
# expect 202 with backend_status=202 — proves the full transform + forward path.
bridge_payload='{"user":"smoke","timestamp":"2026-05-28T11:00:00Z","humidity_weather":70.0,"wind_speed":35.42}'
bridge_out=$(curl -sSL -m "$TIMEOUT" \
    -X POST -H "Content-Type: application/json" \
    -d "$bridge_payload" \
    -w '%{http_code}' -o /tmp/smoke_bridge_body \
    "http://localhost:9090/" 2>&1)
if [[ "$bridge_out" == "202" ]]; then
    printf "${GREEN}✓${RESET} %-32s ${DIM}POST :9090/ — status=202, body=%s${RESET}\n" \
        "agri-bridge end-to-end" "$(cat /tmp/smoke_bridge_body 2>/dev/null | head -c 100)"
    pass=$((pass + 1))
else
    printf "${RED}✗${RESET} %-32s ${DIM}POST :9090/ — status=%s body=%s${RESET}\n" \
        "agri-bridge end-to-end" "$bridge_out" "$(cat /tmp/smoke_bridge_body 2>/dev/null | head -c 200)"
    fail=$((fail + 1))
fi

# Bridge rejection — non-numeric sensor value must 400 (zod-rejected at bridge)
bridge_bad=$(curl -sSL -m "$TIMEOUT" \
    -X POST -H "Content-Type: application/json" \
    -d '{"user":"","not_a_number":"hot"}' \
    -w '%{http_code}' -o /dev/null \
    "http://localhost:9090/" 2>&1)
if [[ "$bridge_bad" == "400" ]]; then
    printf "${GREEN}✓${RESET} %-32s ${DIM}POST :9090/ — status=400 (zod-rejected)${RESET}\n" "agri-bridge (invalid)"
    pass=$((pass + 1))
else
    printf "${RED}✗${RESET} %-32s ${DIM}POST :9090/ — status=%s (expected 400)${RESET}\n" \
        "agri-bridge (invalid)" "$bridge_bad"
    fail=$((fail + 1))
fi

# Bridge health
bridge_health=$(curl -sSL -m 3 -w '%{http_code}' -o /dev/null "http://localhost:9090/health" 2>&1)
if [[ "$bridge_health" == "200" ]]; then
    printf "${GREEN}✓${RESET} %-32s ${DIM}GET :9090/health — status=200${RESET}\n" "agri-bridge health"
    pass=$((pass + 1))
else
    printf "${RED}✗${RESET} %-32s ${DIM}GET :9090/health — status=%s${RESET}\n" \
        "agri-bridge health" "$bridge_health"
    fail=$((fail + 1))
fi

# ChirpStack uplink (Phase 5b). POST a minimal valid v4 uplink event; expect 202.
chirpstack_payload='{"deviceInfo":{"devEui":"0011223344556677","deviceName":"smoke"},"rxInfo":[{"rssi":-85.2,"snr":7.5}],"object":{"airTemperature":{"value":22.5,"unit":"C"}}}'
cs_out=$(curl -sSL -m "$TIMEOUT" \
    -X POST -H "Content-Type: application/json" \
    -d "$chirpstack_payload" \
    -w '%{http_code}' -o /dev/null \
    "${BASE_URL}/api/v1/lorawan/chirpstack/uplink" 2>&1)
if [[ "$cs_out" == "202" ]]; then
    printf "${GREEN}✓${RESET} %-32s ${DIM}POST /api/v1/lorawan/chirpstack/uplink — status=202${RESET}\n" "chirpstack uplink (valid)"
    pass=$((pass + 1))
else
    printf "${RED}✗${RESET} %-32s ${DIM}POST /api/v1/lorawan/chirpstack/uplink — status=%s${RESET}\n" \
        "chirpstack uplink (valid)" "$cs_out"
    fail=$((fail + 1))
fi

# ChirpStack rejection — short DevEUI must 400
cs_bad=$(curl -sSL -m "$TIMEOUT" \
    -X POST -H "Content-Type: application/json" \
    -d '{"deviceInfo":{"devEui":"SHORT"}}' \
    -w '%{http_code}' -o /dev/null \
    "${BASE_URL}/api/v1/lorawan/chirpstack/uplink" 2>&1)
if [[ "$cs_bad" == "400" ]]; then
    printf "${GREEN}✓${RESET} %-32s ${DIM}POST /api/v1/lorawan/chirpstack/uplink — status=400 (pydantic-rejected)${RESET}\n" "chirpstack uplink (invalid)"
    pass=$((pass + 1))
else
    printf "${RED}✗${RESET} %-32s ${DIM}POST /api/v1/lorawan/chirpstack/uplink — status=%s (expected 400)${RESET}\n" \
        "chirpstack uplink (invalid)" "$cs_bad"
    fail=$((fail + 1))
fi

printf "%s\n" "-----------------------------------"
printf "passed=%d  failed=%d\n" "$pass" "$fail"
[[ "$fail" -eq 0 ]] && exit 0 || exit 1
