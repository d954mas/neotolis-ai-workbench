#!/usr/bin/env bash
# install-controller.sh — provision the host-CLI controller (naiw-tasks).
#
# The controller depends on naiw_common (in-repo wire-format library, NOT on
# any package index). pipx's default `pipx install -e ./src/naiw_tasks` would
# fail because pipx creates an isolated venv per tool and would try to
# resolve naiw_common from PyPI. This script wraps the correct multi-editable
# install so both packages land in the same pipx venv.
#
# Usage:
#     bash scripts/install-controller.sh
#
# After install, `naiw-tasks --help` is on PATH (pipx's ~/.local/bin).
# Re-run safely — pipx --force reinstalls if the venv already exists.

set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$here/.." && pwd)"

common_dir="$repo_root/src/naiw_common"
tasks_dir="$repo_root/src/naiw_tasks"

for d in "$common_dir" "$tasks_dir"; do
    [[ -d "$d" ]] || { echo "[install] FAIL: $d not found" >&2; exit 1; }
done

# Prefer uv when present (faster, supports --with for sibling editable),
# fall back to pipx (--pip-args for the same effect), final fallback is a
# plain venv-based pip install for environments lacking both.
if command -v uv >/dev/null 2>&1; then
    echo "[install] using uv tool install"
    uv tool install --force \
        --with-editable "$common_dir" \
        --editable "$tasks_dir"
elif command -v pipx >/dev/null 2>&1; then
    echo "[install] using pipx with --pip-args injection"
    pipx install --force \
        --editable "$tasks_dir" \
        --pip-args "--editable $common_dir"
else
    echo "[install] neither uv nor pipx found; falling back to ~/.naiw-venv"
    venv="$HOME/.naiw-venv"
    python3 -m venv "$venv"
    "$venv/bin/pip" install --upgrade pip
    "$venv/bin/pip" install --editable "$common_dir" --editable "$tasks_dir"
    cat >&2 <<EOF
[install] venv at $venv created.
[install] Either prepend its bin to PATH:
[install]     export PATH="$venv/bin:\$PATH"
[install] or symlink the launcher:
[install]     ln -sf "$venv/bin/naiw-tasks" ~/.local/bin/naiw-tasks
EOF
fi

echo "[install] checking installed binary"
if command -v naiw-tasks >/dev/null 2>&1; then
    naiw-tasks --help | head -5
    echo "[install] OK"
else
    echo "[install] WARNING: naiw-tasks not yet on PATH; see install-tool output above" >&2
fi
