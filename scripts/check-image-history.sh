#!/usr/bin/env bash
# Audit `docker history --no-trunc <image>` for secret-shape strings.
# The build pipeline uses no build-time secrets so the history is clean by
# construction; this script exists to catch accidental regressions
# (e.g., someone embedding a token via ARG by mistake).
set -euo pipefail

if [[ "${1:-}" == "--help" ]]; then
    cat <<'EOF'
Usage: bash scripts/check-image-history.sh [<image>]
  Greps `docker history --no-trunc <image>` for secret-shape patterns:
    ghp_*, gho_*, ghs_*, sk-ant-*, sk-*, AKIA[A-Z0-9]{16}, Bearer ...
  Exits 0 if clean (no match); exits 1 if any pattern matches.
  Default image: naiw-task-image:0.1.0
EOF
    exit 0
fi

image="${1:-naiw-task-image:0.1.0}"

if ! command -v docker >/dev/null 2>&1; then
    echo "check-image-history: docker CLI not on PATH; cannot audit" >&2
    exit 2
fi

if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "check-image-history: image not found: $image (build it first via scripts/build-image.sh)" >&2
    exit 2
fi

# Regex set mirrors the redactor's patterns plus the "Bearer" prefix.
# Any match means the build leaked a secret-shaped string into image metadata.
pattern='ghp_[A-Za-z0-9]{30,}|gho_[A-Za-z0-9]{30,}|ghs_[A-Za-z0-9]{30,}|sk-ant-[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16}|[Bb]earer [A-Za-z0-9._-]{20,}'

if docker history --no-trunc "$image" | grep -E -- "$pattern"; then
    echo "check-image-history: FAIL — secret-shape strings detected in $image history" >&2
    exit 1
fi

echo "check-image-history: ok ($image clean)"
