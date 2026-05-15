#!/usr/bin/env bash
# Reproducible naiw-controller image build helper. Mirrors scripts/build-image.sh.
# The controller image has no Pi runtime, so the Pi-version build arg used by
# scripts/build-image.sh is intentionally absent here. Stamps git SHA into
# LABEL via NAIW_GIT_SHA build-arg.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$here/.." && pwd)"
cd "$repo_root"

NAIW_VERSION="${NAIW_VERSION:-0.1.0}"
NAIW_GIT_SHA="${NAIW_GIT_SHA:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"

if [[ "${1:-}" == "--help" ]]; then
    cat <<EOF
Usage: scripts/build-controller.sh
  Builds naiw-controller:\${NAIW_VERSION} and :latest using BuildKit.
  Reads git SHA via 'git rev-parse HEAD'.
Env:
  NAIW_VERSION   default: 0.1.0
  NAIW_GIT_SHA   default: \$(git rev-parse HEAD)
EOF
    exit 0
fi

echo "naiw-build-controller: NAIW_VERSION=$NAIW_VERSION NAIW_GIT_SHA=$NAIW_GIT_SHA"

DOCKER_BUILDKIT=1 docker build \
    --build-arg "NAIW_VERSION=$NAIW_VERSION" \
    --build-arg "NAIW_GIT_SHA=$NAIW_GIT_SHA" \
    -t "naiw-controller:$NAIW_VERSION" \
    -t "naiw-controller:latest" \
    -t "ghcr.io/d954mas/naiw-controller:$NAIW_VERSION" \
    -t "ghcr.io/d954mas/naiw-controller:latest" \
    -f image/controller.Dockerfile \
    .

# Tag the GHCR ref too so deploy/docker-compose.yml (which pins to
# ghcr.io/d954mas/naiw-controller:latest for production-pull) finds the local
# build without an additional `docker compose pull` step.
echo "naiw-build-controller: built naiw-controller:$NAIW_VERSION + :latest + ghcr.io/d954mas/naiw-controller:$NAIW_VERSION + :latest"
