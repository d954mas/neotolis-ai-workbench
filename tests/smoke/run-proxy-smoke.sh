#!/usr/bin/env bash
# tests/smoke/run-proxy-smoke.sh — Phase 1 proxy smoke (PROXY-02, PROXY-03).
# One positive (containers/json -> 200) + one negative (containers/<id>/exec -> 403).
# Phase 2.5 runs the full grid (IMAGES, VOLUMES, NETWORKS, BUILD, ...).
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$here/../.." && pwd)"
cd "$repo_root"

compose_file="deploy/docker-compose.yml"
sibling="naiw-proxy-smoke-probe-$$"

fail() {
    echo "[proxy-smoke] FAIL: $*" >&2
    docker compose -f "$compose_file" logs naiw-docker-proxy 2>&1 | sed 's/^/[proxy] /' >&2 || true
    exit 1
}

cleanup() {
    docker rm -f "$sibling" >/dev/null 2>&1 || true
    docker compose -f "$compose_file" down >/dev/null 2>&1 || true
}
trap cleanup EXIT

if ! command -v docker >/dev/null 2>&1; then
    echo "[proxy-smoke] SKIP: docker not on PATH"
    exit 0
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "[proxy-smoke] SKIP: docker compose v2 not available"
    exit 0
fi

echo "[proxy-smoke] bringing up proxy"
docker compose -f "$compose_file" up -d

# Wait for healthy.
status="unknown"
for i in $(seq 1 30); do
    status="$(docker inspect --format='{{.State.Health.Status}}' naiw-docker-proxy 2>/dev/null || echo unknown)"
    [[ "$status" == "healthy" ]] && break
    sleep 1
done
[[ "$status" == "healthy" ]] || fail "proxy did not become healthy ($status)"

# PROXY-03: no host port published. Verify by inspecting compose-rendered config.
if docker compose -f "$compose_file" config | grep -E '^\s*ports:'; then
    fail "PROXY-03: compose has 'ports:' mapping (host port leaked)"
fi

# Sibling container on naiw-internal probes the proxy.
# Use curlimages/curl (already on Docker Hub, tiny). One positive + one negative.

echo "[proxy-smoke] probe 1/2: GET /containers/json (expect 200)"
code="$(docker run --rm --name "$sibling" --network naiw-internal curlimages/curl:latest \
    -s -o /dev/null -w '%{http_code}' \
    http://naiw-docker-proxy:2375/v1.43/containers/json || true)"
[[ "$code" == "200" ]] || fail "PROXY-02 positive: GET /containers/json returned $code, expected 200"

echo "[proxy-smoke] probe 2/2: POST /containers/fakeid/exec (expect 403)"
code="$(docker run --rm --name "$sibling" --network naiw-internal curlimages/curl:latest \
    -s -o /dev/null -w '%{http_code}' -X POST \
    -H 'Content-Type: application/json' \
    -d '{"Cmd":["echo","x"]}' \
    http://naiw-docker-proxy:2375/v1.43/containers/fakeid/exec || true)"
[[ "$code" == "403" ]] || fail "PROXY-02 negative: POST /containers/fakeid/exec returned $code, expected 403"

echo "[proxy-smoke] ok (positive=200, negative=403; full grid runs in Phase 2.5)"
