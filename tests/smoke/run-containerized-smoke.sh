#!/usr/bin/env bash
# tests/smoke/run-containerized-smoke.sh
#
# Containerized-loop smoke harness. Brings up deploy/docker-compose.yml,
# verifies the isolation model (proxy unpublished, controller in naiw-internal,
# wrapper-equivalent `compose run` works), then tears down.
#
# COMPOSE_FILE handling:
#   - If $COMPOSE_FILE is set (CI multi-file stacking like
#     "deploy/docker-compose.yml:deploy/compose.override.yml"), the harness
#     DOES NOT pass `-f`. `docker compose` reads $COMPOSE_FILE natively and
#     supports the `:`-separated multi-file form on Linux.
#   - Otherwise, defaults to the in-repo deploy/docker-compose.yml.
#   So the harness works for both local dev (no env) and CI (env-stacked).
#
# Python3 is required ONLY for one JSON parse below (controller image ref
# from `docker compose config --format json`). It is NOT a runtime requirement
# on operator hosts running `naiw-tasks` — those only need Docker.
#
# SKIP-on-non-Linux. Manual happy-path attach gate (TTY + Ctrl-P Ctrl-Q +
# SIGWINCH known-broken) lives in tests/smoke/HARDENED-CHECKLIST.md.

set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$here/../.." && pwd)"

LIB_LOG_PREFIX="containerized-smoke"
_lib_log_prefix="[${LIB_LOG_PREFIX}]"

# shellcheck source=_lib.sh
source "$here/_lib.sh"

# Resolve compose-file args. When $COMPOSE_FILE is set (CI multi-file stacking),
# rely on docker compose's native env-var support and pass no -f flags. When
# unset (local dev), pin to the in-repo single file via -f.
if [[ -n "${COMPOSE_FILE:-}" ]]; then
    COMPOSE_ARGS=()
    echo "[${LIB_LOG_PREFIX}] using env-var COMPOSE_FILE=${COMPOSE_FILE}"
else
    COMPOSE_ARGS=(-f "${repo_root}/deploy/docker-compose.yml")
    echo "[${LIB_LOG_PREFIX}] using default compose file ${repo_root}/deploy/docker-compose.yml"
fi

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "[${LIB_LOG_PREFIX}] SKIP: requires Linux (got $(uname -s))"
    exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "[${LIB_LOG_PREFIX}] FAIL: docker CLI not on PATH" >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "[${LIB_LOG_PREFIX}] FAIL: docker compose plugin not available" >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    # Step 03 parses `docker compose config --format json` via python3 for
    # the one-line image-reference extraction. Surface the missing dep up
    # front rather than at the pipe with a "command not found".
    echo "[${LIB_LOG_PREFIX}] FAIL: python3 required for compose-config JSON parsing" >&2
    exit 1
fi

SMOKE_STATUS=FAIL
tmp_data="$(mktemp -d)"

teardown() {
    if [[ "$SMOKE_STATUS" == "PASS" ]]; then
        rm -rf "$tmp_data"
        docker compose "${COMPOSE_ARGS[@]}" down >/dev/null 2>&1 || true
        echo "[${LIB_LOG_PREFIX}] PASS — stack torn down"
    else
        echo "[${LIB_LOG_PREFIX}] FAIL — preserving stack for inspection" >&2
        echo "[${LIB_LOG_PREFIX}] inspect: docker compose ${COMPOSE_ARGS[*]} ps -a" >&2
        echo "[${LIB_LOG_PREFIX}] data: $tmp_data" >&2
    fi
}
trap teardown EXIT

# Step 01: bring up the stack. Controller is one-shot so `up -d` only creates it;
# `compose run` is the operator path. Proxy starts and stays up.
# naiw-task-net is `external: true` in compose — create BEFORE any compose call,
# otherwise compose may refuse to read the file with "external network not found".
docker network inspect naiw-task-net >/dev/null 2>&1 \
    || docker network create naiw-task-net >/dev/null
echo "[${LIB_LOG_PREFIX}] 01: docker compose up -d naiw-docker-proxy"
docker compose "${COMPOSE_ARGS[@]}" up -d naiw-docker-proxy

# Step 02: proxy must NOT have published ports.
echo "[${LIB_LOG_PREFIX}] 02: proxy unpublished"
proxy_ports="$(docker inspect naiw-docker-proxy --format '{{json .HostConfig.PortBindings}}' 2>/dev/null || echo 'null')"
case "$proxy_ports" in
    "{}"|"null"|"")
        echo "[${LIB_LOG_PREFIX}]     OK: PortBindings=$proxy_ports"
        ;;
    *)
        echo "[${LIB_LOG_PREFIX}]     FAIL: proxy must not publish a host port; PortBindings=$proxy_ports" >&2
        exit 1
        ;;
esac

# Step 03: controller image carries the expected labels.
echo "[${LIB_LOG_PREFIX}] 03: controller image labels"
ctrl_image="$(docker compose "${COMPOSE_ARGS[@]}" config --format json | python3 -c \
    "import sys, json; d=json.load(sys.stdin); print(d['services']['naiw-controller']['image'])")"
if ! docker pull "$ctrl_image" >/dev/null 2>&1; then
    echo "[${LIB_LOG_PREFIX}]     note: 'docker pull $ctrl_image' failed (image may be local-only); using on-daemon copy"
fi
labels="$(docker inspect "$ctrl_image" --format '{{json .Config.Labels}}')"
for required in '"naiw.managed":"1"' '"naiw.role":"controller"' '"naiw.docker-api-version":"1.43"' '"org.opencontainers.image.source"'; do
    if [[ "$labels" != *"$required"* ]]; then
        echo "[${LIB_LOG_PREFIX}]     FAIL: label $required missing" >&2
        echo "[${LIB_LOG_PREFIX}]     got: $labels" >&2
        exit 1
    fi
done
echo "[${LIB_LOG_PREFIX}]     OK: required labels present"

# Step 04: `doctor` runs the real CLI callback and startup checks.
echo "[${LIB_LOG_PREFIX}] 04: compose run --rm naiw-controller doctor"
mkdir -p "$tmp_data/secrets" "$tmp_data/pi-packages" "$tmp_data/workspace/repos" "$tmp_data/tasks"
chmod 0700 "$tmp_data/secrets"
doctor_out="$(docker compose "${COMPOSE_ARGS[@]}" run --rm \
    -T \
    --user "$(id -u):$(id -g)" \
    -e "NAIW_DATA=/naiw-data" \
    -v "$tmp_data:/naiw-data" \
    naiw-controller doctor 2>&1)"
# doctor now prints sectioned output: `[startup_checks]\n  OK\n[disk]\n...`.
# The startup_checks line proves the proxy / controller / data_root chain is
# wired correctly — the same fact the old `doctor OK` placeholder asserted.
if [[ "$doctor_out" != *"[startup_checks]"* ]] || [[ "$doctor_out" != *"OK"* ]]; then
    echo "[${LIB_LOG_PREFIX}]     FAIL: doctor output unexpected" >&2
    echo "[${LIB_LOG_PREFIX}]     got: $doctor_out" >&2
    exit 1
fi
echo "[${LIB_LOG_PREFIX}]     OK: doctor reached the controller and proxy"

# Step 05: generic start+finish exercises the real lifecycle path: task network,
# task image, bind mounts, container create/start/stop/remove through the proxy.
echo "[${LIB_LOG_PREFIX}] 05: generic start + finish"
task_image="ghcr.io/d954mas/naiw-task-image:latest"
if docker image inspect naiw-task-image:latest >/dev/null 2>&1; then
    task_image="naiw-task-image:latest"
fi
cat >"$tmp_data/config.yaml" <<EOF
schema_version: 1
task_image: ${task_image}
EOF
start_out="$(docker compose "${COMPOSE_ARGS[@]}" run --rm \
    -T \
    --user "$(id -u):$(id -g)" \
    -e "NAIW_DATA=/naiw-data" \
    -e "NAIW_DATA_HOST=$tmp_data" \
    -v "$tmp_data:/naiw-data" \
    naiw-controller start 2>&1)"
if [[ "$start_out" != *"started naiw-task-task-001 (status=running)"* ]]; then
    echo "[${LIB_LOG_PREFIX}]     FAIL: start output unexpected" >&2
    echo "[${LIB_LOG_PREFIX}]     got: $start_out" >&2
    exit 1
fi

# Verify bind sources on the task container resolve to host paths, not the
# in-container /naiw-data path. If the daemon got /naiw-data/... it would
# silently create empty host dirs and Pi would mount nothing.
for bind in /work /io /pi-packages; do
    src="$(docker inspect naiw-task-task-001 \
        --format "{{range .Mounts}}{{if eq .Destination \"${bind}\"}}{{.Source}}{{end}}{{end}}")"
    if [[ -z "$src" ]]; then
        echo "[${LIB_LOG_PREFIX}]     FAIL: task container has no mount at ${bind}" >&2
        exit 1
    fi
    case "$src" in
        "$tmp_data"/*|"$tmp_data")
            ;;
        *)
            echo "[${LIB_LOG_PREFIX}]     FAIL: ${bind} mount source = $src" >&2
            echo "[${LIB_LOG_PREFIX}]     expected prefix: $tmp_data (host path)" >&2
            exit 1
            ;;
    esac
done

docker compose "${COMPOSE_ARGS[@]}" run --rm \
    -T \
    --user "$(id -u):$(id -g)" \
    -e "NAIW_DATA=/naiw-data" \
    -e "NAIW_DATA_HOST=$tmp_data" \
    -v "$tmp_data:/naiw-data" \
    naiw-controller finish task-001 >/dev/null
if docker inspect naiw-task-task-001 >/dev/null 2>&1; then
    echo "[${LIB_LOG_PREFIX}]     FAIL: task container still exists after finish" >&2
    exit 1
fi
echo "[${LIB_LOG_PREFIX}]     OK: generic task lifecycle works"

SMOKE_STATUS=PASS
