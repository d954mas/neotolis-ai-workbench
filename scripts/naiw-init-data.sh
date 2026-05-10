#!/usr/bin/env bash
# scripts/naiw-init-data.sh — idempotent host data-layout bootstrap (D-19 / DATA-01 / DATA-02).
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "--help" ]]; then
    cat <<'EOF'
Usage: bash scripts/naiw-init-data.sh
  Idempotently bootstraps the NAIW host data layout under ~/naiw-data/ (or $NAIW_DATA).
  Creates: secrets/ (0700), pi-packages/, workspace/repos/, tasks/, projects.yaml.
  Re-running is a no-op; an existing projects.yaml is NEVER overwritten.
Env:
  NAIW_DATA   default: ~/naiw-data   (override for tests)
EOF
    exit 0
fi

data_root="${NAIW_DATA:-$HOME/naiw-data}"

mkdir -p \
    "$data_root/secrets" \
    "$data_root/pi-packages" \
    "$data_root/workspace/repos" \
    "$data_root/tasks"

# D-19 / DATA-02: secrets/ MUST be mode 0700. mkdir -p uses umask (~0755), so explicit chmod.
chmod 0700 "$data_root/secrets"

# D-19 / Pitfall 9: copy example projects.yaml ONLY if destination does not exist.
example_src="$here/projects.yaml.example"
projects_dst="$data_root/projects.yaml"
if [[ ! -e "$projects_dst" ]]; then
    if [[ ! -f "$example_src" ]]; then
        echo "naiw-init-data: missing $example_src (repo broken?)" >&2
        exit 1
    fi
    cp "$example_src" "$projects_dst"
    copied="created"
else
    copied="preserved existing"
fi

echo "naiw-init-data: ok ($data_root; secrets=0700; projects.yaml $copied)"
