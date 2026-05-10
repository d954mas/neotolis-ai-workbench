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

# Wait for the proxy to respond on its expected port.
# NOTE: We DO NOT wait for `healthy` status here. The compose healthcheck targets
# /_ping, but the locked allowlist sets PING=0 (paranoid-explicit) which makes
# the proxy correctly return 403 to that path — so the container never reports
# `healthy`. This is a Plan 02 healthcheck/allowlist mismatch (documented as a
# Phase 1 deferred item); the proxy itself is working. We poll a simple allowed
# endpoint via a sibling container instead.
echo "[proxy-smoke] waiting for proxy to accept allowed requests"
ready=0
for i in $(seq 1 30); do
    code="$(docker run --rm --network naiw-internal curlimages/curl:latest \
        -s -o /dev/null -w '%{http_code}' \
        --max-time 2 \
        http://naiw-docker-proxy:2375/v1.43/containers/json 2>/dev/null || echo "000")"
    if [[ "$code" == "200" ]]; then
        ready=1
        break
    fi
    sleep 1
done
[[ "$ready" -eq 1 ]] || fail "proxy did not accept allowed requests within 30s"

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

# NOTE on negative probe choice:
# The plan originally specified POST /containers/<fakeid>/exec for the negative
# probe, but Tecnativa's EXEC env var gates the /exec/* path family
# (POST /exec/<id>/start, GET /exec/<id>/json), NOT the /containers/<id>/exec
# CREATE endpoint (which is gated by CONTAINERS+POST and returns 201 with the
# allowlist as written). The truly EXEC=0-gated path is POST /exec/<id>/start
# — verified empirically to return 403 on the locked allowlist.
echo "[proxy-smoke] probe 2/2: POST /exec/fakeid/start (expect 403; EXEC=0 lock)"
code="$(docker run --rm --name "$sibling" --network naiw-internal curlimages/curl:latest \
    -s -o /dev/null -w '%{http_code}' -X POST \
    -H 'Content-Type: application/json' \
    -d '{}' \
    http://naiw-docker-proxy:2375/v1.43/exec/fakeid/start || true)"
[[ "$code" == "403" ]] || fail "PROXY-02 negative: POST /exec/fakeid/start returned $code, expected 403 (EXEC=0)"

echo "[proxy-smoke] ok (positive=200, negative=403; full grid runs in Phase 2.5)"
