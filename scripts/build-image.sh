#!/usr/bin/env bash
# scripts/build-image.sh — reproducible naiw-task-image build (D-28, D-29).
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$here/.." && pwd)"
cd "$repo_root"

NAIW_VERSION="${NAIW_VERSION:-0.1.0}"
PI_VERSION="${PI_VERSION:-$(cat image/PI_VERSION)}"
NAIW_GIT_SHA="${NAIW_GIT_SHA:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"

if [[ "${1:-}" == "--help" ]]; then
    cat <<EOF
Usage: scripts/build-image.sh
  Builds naiw-task-image:\${NAIW_VERSION} and :latest using BuildKit.
  Reads PI_VERSION from image/PI_VERSION (override with PI_VERSION env var).
  Reads git SHA via 'git rev-parse HEAD'.
Env:
  NAIW_VERSION   default: 0.1.0
  PI_VERSION     default: \$(cat image/PI_VERSION)
  NAIW_GIT_SHA   default: \$(git rev-parse HEAD)
EOF
    exit 0
fi

echo "naiw-build: NAIW_VERSION=$NAIW_VERSION PI_VERSION=$PI_VERSION NAIW_GIT_SHA=$NAIW_GIT_SHA"

DOCKER_BUILDKIT=1 docker build \
    --build-arg "PI_VERSION=$PI_VERSION" \
    --build-arg "NAIW_VERSION=$NAIW_VERSION" \
    --build-arg "NAIW_GIT_SHA=$NAIW_GIT_SHA" \
    -t "naiw-task-image:$NAIW_VERSION" \
    -t "naiw-task-image:latest" \
    -f image/Dockerfile \
    .

echo "naiw-build: built naiw-task-image:$NAIW_VERSION and :latest"
