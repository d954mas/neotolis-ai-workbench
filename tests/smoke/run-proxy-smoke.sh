#!/usr/bin/env bash
# Proxy smoke harness: minimum positive+negative pair for early-warning detection
# of proxy misconfigurations. One positive (containers/json -> 200) + one
# negative (exec/<id>/start -> 403). The full per-endpoint grid lives in the
# hardened-lifecycle smoke suite.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$here/../.." && pwd)"
cd "$repo_root"

_lib_log_prefix="[proxy-smoke]"
# shellcheck source=_lib.sh
source "$here/_lib.sh"

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

# naiw-task-net is `external: true` in deploy/docker-compose.yml; compose may
# refuse to read the file with an "external network not found" error before
# even bringing up the proxy. Pre-create idempotently.
docker network inspect naiw-task-net >/dev/null 2>&1 \
    || docker network create naiw-task-net >/dev/null

echo "[proxy-smoke] bringing up proxy"
docker compose -f "$compose_file" up -d

# Wait for the proxy to respond on its expected port. We deliberately do not
# wait for `healthy` status: there is no compose healthcheck (PING=0 makes
# /_ping return 403, so any HTTP probe would conflict with the allowlist).
# Instead we poll an allow-listed endpoint via a sibling container.
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

# Verify the proxy publishes NO host port. Controller reaches the proxy via
# the naiw-internal network, not a host-bound port; any host PortBinding
# would re-expose Docker control surface to other same-host processes.
echo "[proxy-smoke] verifying proxy has no published host port"
proxy_ports="$(docker inspect naiw-docker-proxy --format '{{json .HostConfig.PortBindings}}' 2>/dev/null || echo 'null')"
case "$proxy_ports" in
    "{}"|"null"|"")
        echo "[proxy-smoke] OK: PortBindings=$proxy_ports"
        ;;
    *)
        fail "proxy has unexpected host PortBindings=$proxy_ports"
        ;;
esac

# Compose-source check: any non-localhost `ports:` entry on any service in
# deploy/docker-compose.yml is a security regression. Accepts only 127.0.0.1 /
# [::1] prefixes. The naiw-docker-proxy service has no `ports:` block;
# this scan stays as defense-in-depth if a future service adds one.
suspicious_ports="$(awk '
    in_ports && /^[[:space:]]+-[[:space:]]/ {
        line=$0
        sub(/^[[:space:]]+-[[:space:]]*"?/, "", line)
        sub(/"?[[:space:]]*$/, "", line)
        if (line !~ /^127\.0\.0\.1:/ && line !~ /^\[::1\]:/) print line
        next
    }
    in_ports { in_ports=0 }
    /^[[:space:]]+ports:[[:space:]]*$/ { in_ports=1 }
' "$compose_file" || true)"
if [[ -n "$suspicious_ports" ]]; then
    fail "compose has non-localhost port mapping (LAN leak): $suspicious_ports"
fi

# Sibling container on naiw-internal probes the proxy.
# curlimages/curl is tiny and on Docker Hub. One positive + one negative.

echo "[proxy-smoke] probe 1/2: GET /containers/json (expect 200)"
probe_proxy_endpoint GET /containers/json 200 "GET /containers/json" \
    || fail "GET /containers/json positive probe failed"

# Negative probe: POST /exec/<id>/start. Tecnativa's EXEC env var gates the
# /exec/* path family — NOT /containers/<id>/exec (which is the CREATE endpoint
# gated by CONTAINERS+POST and returns 201 with the allowlist as written).
echo "[proxy-smoke] probe 2/2: POST /exec/fakeid/start (expect 403; EXEC=0 lock)"
probe_proxy_endpoint POST /exec/fakeid/start 403 "POST /exec/fakeid/start" \
    || fail "POST /exec/fakeid/start negative probe failed"

echo "[proxy-smoke] ok (positive=200, negative=403)"
