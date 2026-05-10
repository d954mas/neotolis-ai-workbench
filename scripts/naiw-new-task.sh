#!/usr/bin/env bash
# scripts/naiw-new-task.sh <task-id> — per-task directory skeleton (D-20 / DATA-03 / DATA-06).
# Does NOT write task.json (Phase 3 controller territory).
set -euo pipefail

if [[ "${1:-}" == "--help" || $# -eq 0 ]]; then
    cat <<'EOF'
Usage: bash scripts/naiw-new-task.sh <task-id>
  Creates ~/naiw-data/tasks/<task-id>/{meta,work,io,io/.naiw}/.
  <task-id> must match ^[a-z0-9][a-z0-9-]{0,63}$ (CTRL-03).
  Refuses to overwrite an existing task directory.
  Does NOT write task.json — that is the controller's responsibility (Phase 3+).
Env:
  NAIW_DATA   default: ~/naiw-data   (override for tests)
EOF
    # --help is success, missing arg is failure.
    [[ "${1:-}" == "--help" ]] && exit 0
    exit 2
fi

task_id="$1"

if ! [[ "$task_id" =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]]; then
    echo "naiw-new-task: invalid task id: $task_id (must match ^[a-z0-9][a-z0-9-]{0,63}\$)" >&2
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
    "$target/io/.naiw"

echo "naiw-new-task: created $target/{meta,work,io,io/.naiw}"
