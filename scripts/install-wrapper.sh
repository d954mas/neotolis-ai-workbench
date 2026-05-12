#!/usr/bin/env bash
# install-wrapper.sh — provision the containerized controller wrapper.
#
# After this runs, `naiw-tasks <subcommand>` is on the operator's PATH and
# delegates every call to:
#     docker compose -f /etc/naiw/docker-compose.yml \
#         run --rm -it \
#         --user "$(id -u):$(id -g)" \
#         -e NAIW_DATA -e NAIW_ACCEPT_WINDOWS_FS_RISK \
#         naiw-controller \
#         <subcommand> <args>
#
# Prereqs:
#   - Docker + the docker-compose plugin; operator user in `docker` group on Linux.
#   - /etc/naiw/docker-compose.yml installed:
#         sudo install -d /etc/naiw
#         sudo cp deploy/docker-compose.yml /etc/naiw/docker-compose.yml
#   - `docker compose -f /etc/naiw/docker-compose.yml pull` once at install time
#     (CI workflow build-images.yml from P05 publishes naiw-controller to ghcr).
#
# If you previously ran the host-CLI installer (deprecated `scripts/install-controller.sh`,
# now removed), uninstall the pipx package first:
#     pipx uninstall naiw_tasks  # or: uv tool uninstall naiw_tasks
# The previous phase was not deployed to production (CONTEXT.md D-M1 overrides ROADMAP SC #5),
# so a separate migration doc is intentionally not provided.

set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$here/.." && pwd)"

WRAPPER_PATH="${HOME}/.local/bin/naiw-tasks"
COMPOSE_FILE_SYSTEM="/etc/naiw/docker-compose.yml"
TASK_IMAGE_REF="ghcr.io/d954mas/naiw-task-image:latest"

if [[ "${1:-}" == "--help" ]]; then
    cat <<EOF
Usage: scripts/install-wrapper.sh
  Installs ${WRAPPER_PATH} as a bash wrapper around 'docker compose run'.
  Requires ${COMPOSE_FILE_SYSTEM} to exist (operator copies via sudo).
EOF
    exit 0
fi

# Pitfall 8 — running install-wrapper.sh under sudo would set HOME=/root and
# bake /root/.local/bin into the operator's wrapper.
if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    echo "[install] ERROR: do not run install-wrapper.sh as root" >&2
    echo "[install]   The wrapper relies on \$HOME and \$NAIW_DATA from the" >&2
    echo "[install]   invoking user's shell; running under sudo sets HOME=/root." >&2
    echo "[install]   Re-run as the operator user; add yourself to the docker group instead." >&2
    exit 2
fi

if [[ ! -f "$COMPOSE_FILE_SYSTEM" ]]; then
    echo "[install] ERROR: compose file not found at $COMPOSE_FILE_SYSTEM" >&2
    echo "[install] Install it first:" >&2
    echo "[install]   sudo install -d /etc/naiw" >&2
    echo "[install]   sudo cp $repo_root/deploy/docker-compose.yml $COMPOSE_FILE_SYSTEM" >&2
    echo "[install] Then re-run scripts/install-wrapper.sh." >&2
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

mkdir -p "${HOME}/.local/bin"

# Single-quoted heredoc — every $VAR resolves at the OPERATOR's shell-runtime,
# never at install time. An unquoted <<WRAPPER would bake the installer's HOME
# and NAIW_DATA into the wrapper, which is the critical bug this guards against.
cat >"$WRAPPER_PATH" <<'WRAPPER'
#!/usr/bin/env bash
# ~/.local/bin/naiw-tasks — containerized controller wrapper.
# Delegates every invocation to a one-shot `docker compose run --rm -it`
# against the naiw-controller service in the system compose file.
#
# Env knobs the operator can set in their shell:
#   NAIW_DATA                    bind-mount source on the host (default ~/naiw-data).
#                                Consumed by COMPOSE'S volume substitution
#                                (${NAIW_DATA:-${HOME}/naiw-data}:/naiw-data),
#                                NOT forwarded into the container — inside the
#                                container NAIW_DATA stays /naiw-data per the
#                                service-level env in deploy/docker-compose.yml.
#   NAIW_ACCEPT_WINDOWS_FS_RISK  set to "1" to opt in on WSL2 /mnt/<letter>/ paths
#   NAIW_DOCKER_PROXY_URL        override proxy URL (config.py reads this env)
#   NAIW_COMPOSE_FILE            override the compose file path (default /etc/naiw/docker-compose.yml)
set -euo pipefail

COMPOSE_FILE="${NAIW_COMPOSE_FILE:-/etc/naiw/docker-compose.yml}"

if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "naiw-tasks: compose file not found at $COMPOSE_FILE" >&2
    echo "  re-run scripts/install-wrapper.sh, or set NAIW_COMPOSE_FILE" >&2
    exit 2
fi

# Compose reads $NAIW_DATA from the wrapper's env to resolve the bind-mount
# source in `volumes:`. `exec docker compose` inherits this env transparently,
# so the operator's `export NAIW_DATA=...` (or absence) flows through.
#
# CRITICAL: do NOT pass `-e NAIW_DATA=...` into the container. The container's
# NAIW_DATA must stay `/naiw-data` (the mount target, set by the service-level
# `environment:` block in deploy/docker-compose.yml). `compose run -e` OVERRIDES
# service-level env, so forwarding the host path here would make naiw_tasks
# look for /home/operator/naiw-data inside the container — which does not
# exist (the mount lives at /naiw-data).
#
# Operator-tunable env vars forwarded via `-e VAR_NAME` (no `=value`): compose
# reads the current value from the wrapper's env if set, or skips the var
# entirely if unset, which lets the service-level defaults stand.
exec docker compose -f "$COMPOSE_FILE" \
    run --rm -it \
    --user "$(id -u):$(id -g)" \
    -e NAIW_ACCEPT_WINDOWS_FS_RISK \
    -e NAIW_DOCKER_PROXY_URL \
    naiw-controller \
    "$@"
WRAPPER

chmod 0755 "$WRAPPER_PATH"
echo "[install] wrote $WRAPPER_PATH"

# Pitfall 5 — pre-pull the task image so the first `naiw-tasks start` doesn't
# block on a cold GHCR fetch on a fresh VPS. Non-fatal: GHCR may be unreachable
# (private repo / network policy / CI not yet published) and the first start
# will retry the pull anyway.
echo "[install] pulling $TASK_IMAGE_REF"
if ! docker pull "$TASK_IMAGE_REF"; then
    echo "[install] WARN: failed to pull $TASK_IMAGE_REF" >&2
    echo "[install]   First 'naiw-tasks start' will retry the pull; ensure ghcr is reachable." >&2
fi

echo "[install] OK — ensure ${HOME}/.local/bin is on \$PATH"
if [[ ":${PATH}:" != *":${HOME}/.local/bin:"* ]]; then
    echo "[install] WARN: ${HOME}/.local/bin is not on \$PATH" >&2
    echo "[install]   add 'export PATH=\"\$HOME/.local/bin:\$PATH\"' to your shell rc" >&2
fi
