#!/usr/bin/env bash
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$here/.." && pwd)"

WRAPPER_PATH="${HOME}/.local/bin/naiw-tasks"
COMPOSE_FILE_SYSTEM="/etc/naiw/docker-compose.yml"
TASK_IMAGE_REF="ghcr.io/d954mas/naiw-task-image:latest"

if [[ "${1:-}" == "--help" ]]; then
    cat <<EOF
Usage: scripts/install-wrapper.sh
  Installs ${WRAPPER_PATH} as a docker-compose wrapper.
  Creates naiw-task-net if missing.
  Requires ${COMPOSE_FILE_SYSTEM} to exist.
EOF
    exit 0
fi

if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    echo "[install] ERROR: do not run install-wrapper.sh as root" >&2
    echo "[install] Re-run as the operator user; add that user to the docker group." >&2
    exit 2
fi

if [[ ! -f "$COMPOSE_FILE_SYSTEM" ]]; then
    echo "[install] ERROR: compose file not found at $COMPOSE_FILE_SYSTEM" >&2
    echo "[install] Install it first:" >&2
    echo "[install]   sudo install -d /etc/naiw" >&2
    echo "[install]   sudo cp $repo_root/deploy/docker-compose.yml $COMPOSE_FILE_SYSTEM" >&2
    exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "[install] ERROR: 'docker' CLI not on PATH" >&2
    exit 2
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "[install] ERROR: 'docker compose' plugin not available" >&2
    exit 2
fi

if ! docker network inspect naiw-task-net >/dev/null 2>&1; then
    echo "[install] creating naiw-task-net (bridge)"
    docker network create naiw-task-net >/dev/null
fi

mkdir -p "${HOME}/.local/bin"

cat >"$WRAPPER_PATH" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${NAIW_COMPOSE_FILE:-/etc/naiw/docker-compose.yml}"
DATA_SRC="${NAIW_DATA:-${HOME}/naiw-data}"

case "$DATA_SRC" in
    "~") DATA_SRC="$HOME" ;;
    "~/"*) DATA_SRC="$HOME/${DATA_SRC#~/}" ;;
esac

if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "naiw-tasks: compose file not found at $COMPOSE_FILE" >&2
    echo "  re-run scripts/install-wrapper.sh, or set NAIW_COMPOSE_FILE" >&2
    exit 2
fi

if [[ ! -d "$DATA_SRC" ]]; then
    echo "naiw-tasks: data root not found at $DATA_SRC" >&2
    echo "  run scripts/naiw-init-data.sh, or set NAIW_DATA to an existing directory" >&2
    exit 2
fi

if [[ -L "$DATA_SRC" ]]; then
    echo "naiw-tasks: ~/naiw-data/ must not be a symlink (got: $DATA_SRC)" >&2
    exit 2
fi

if ! DATA_REAL="$(realpath -m -- "$DATA_SRC" 2>/dev/null)"; then
    DATA_REAL="$DATA_SRC"
fi

if [[ "$(uname -s)" == "Linux" && "$DATA_REAL" =~ ^/mnt/[A-Za-z]/ ]]; then
    if [[ "${NAIW_ACCEPT_WINDOWS_FS_RISK:-}" != "1" ]]; then
        echo "naiw-tasks: ~/naiw-data/ on Windows-FS path '$DATA_REAL' is not supported" >&2
        echo "  set NAIW_ACCEPT_WINDOWS_FS_RISK=1 only for single-task local dev" >&2
        exit 2
    fi
    marker="$DATA_REAL/.naiw-winfs-acked"
    if [[ ! -e "$marker" ]]; then
        echo "naiw-tasks: WARNING - ~/naiw-data/ on Windows-FS path '$DATA_REAL'; local dev only" >&2
        touch "$marker" 2>/dev/null || true
    fi
fi

export NAIW_DATA="$DATA_REAL"

if [[ -t 0 && -t 1 ]]; then
    tty_args=(-i -t)
else
    tty_args=(-T)
fi

exec docker compose -f "$COMPOSE_FILE" \
    run --rm "${tty_args[@]}" \
    --user "$(id -u):$(id -g)" \
    -e NAIW_ACCEPT_WINDOWS_FS_RISK \
    naiw-controller \
    "$@"
WRAPPER

chmod 0755 "$WRAPPER_PATH"
echo "[install] wrote $WRAPPER_PATH"

echo "[install] pulling controller + proxy images via docker compose"
if ! docker compose -f "$COMPOSE_FILE_SYSTEM" pull; then
    echo "[install] WARN: docker compose pull failed" >&2
    echo "[install]   If GHCR is private, run 'docker login ghcr.io' and retry." >&2
fi

echo "[install] pulling task image $TASK_IMAGE_REF"
if ! docker pull "$TASK_IMAGE_REF"; then
    echo "[install] WARN: failed to pull $TASK_IMAGE_REF" >&2
    echo "[install]   The task image must be present before 'naiw-tasks start'." >&2
    echo "[install]   Retry once GHCR is reachable: docker pull $TASK_IMAGE_REF" >&2
fi

echo "[install] OK - ensure ${HOME}/.local/bin is on \$PATH"
if [[ ":${PATH}:" != *":${HOME}/.local/bin:"* ]]; then
    echo "[install] WARN: ${HOME}/.local/bin is not on \$PATH" >&2
    echo "[install]   add 'export PATH=\"\$HOME/.local/bin:\$PATH\"' to your shell rc" >&2
fi
