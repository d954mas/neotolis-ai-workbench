#!/usr/bin/env bash
# Per-task directory skeleton creator. Does NOT write task.json — the controller
# owns that file.
set -euo pipefail

if [[ "${1:-}" == "--help" || $# -eq 0 ]]; then
    cat <<'EOF'
Usage: bash scripts/naiw-new-task.sh <task-id>
  Creates ~/naiw-data/tasks/<task-id>/{meta,work,io,io/.naiw,storage}/.
  <task-id> must match ^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$ (DNS-label style;
  alphanumeric first AND last character, lowercase, 1-64 chars).
  Refuses to overwrite an existing task directory.
  Does NOT write task.json — the controller owns that file.
Env:
  NAIW_DATA   default: ~/naiw-data   (override for tests)
EOF
    # --help is success, missing arg is failure.
    [[ "${1:-}" == "--help" ]] && exit 0
    exit 2
fi

task_id="$1"

if ! [[ "$task_id" =~ ^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$ ]]; then
    echo "naiw-new-task: invalid task id: $task_id (must match ^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?\$)" >&2
    exit 1
fi

data_root="${NAIW_DATA:-$HOME/naiw-data}"

if [[ ! -d "$data_root" ]]; then
    echo "naiw-new-task: data root not found: $data_root (run scripts/naiw-init-data.sh first)" >&2
    exit 1
fi

target="$data_root/tasks/$task_id"

if [[ -e "$target" ]]; then
    echo "naiw-new-task: task already exists: $target" >&2
    exit 1
fi

mkdir -p \
    "$target/meta" \
    "$target/work" \
    "$target/io" \
    "$target/io/.naiw" \
    "$target/storage"

# Sticky-writable on every bind-mount source so pi uid 1000 inside the
# container can write regardless of the operator's host uid.
chmod 1777 "$target/storage" "$target/io" "$target/io/.naiw"

echo "naiw-new-task: created $target/{meta,work,io,io/.naiw,storage}"
