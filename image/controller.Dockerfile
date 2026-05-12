# syntax=docker/dockerfile:1
# Phase 3.5 controller image. Multi-stage: builder produces wheels for
# naiw_common + naiw_tasks; runtime installs them plus git and docker-ce-cli
# (for `os.execvpe attach` from naiw_tasks/attach.py).
#
# Mirrors image/Dockerfile (Phase 1 task image) so the project stays internally
# consistent: same builder/runtime split, same LABEL block shape, same useradd
# pattern, same env hygiene (UTF-8 + DEBIAN_FRONTEND).
#
# This image is invoked one-shot per `naiw-tasks` call by the host wrapper
# (scripts/install-wrapper.sh / Phase 3.5 P03) via:
#   docker compose run --rm -it --user "$(id -u):$(id -g)" naiw-controller ...
# The compose service definition (deploy/docker-compose.yml / Phase 3.5 P02)
# applies the hardening flags (cap_drop ALL, read_only, tmpfs, pids/cpu/mem).

ARG NAIW_VERSION=0.1.0
ARG NAIW_GIT_SHA=unknown

# ── Stage 1: builder — produce wheels for naiw_common and naiw_tasks ──
FROM python:3.12-slim-bookworm AS builder
WORKDIR /build
RUN pip install --no-cache-dir build==1.2.*
COPY src/naiw_common/ ./naiw_common/
COPY src/naiw_tasks/ ./naiw_tasks/
RUN python -m build --wheel --outdir /wheels ./naiw_common && \
    python -m build --wheel --outdir /wheels ./naiw_tasks

# ── Stage 2: runtime — python:3.12-slim-bookworm (tag form for this plan) ──
# To refresh the base image digest:
#   docker pull python:3.12-slim-bookworm
#   docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim-bookworm
# The @sha256:... digest pin is intentionally deferred to Phase 3.5 P05 (ghcr
# build-push workflow) — CI resolves and stamps the digest as part of the first
# published build. Until then the tag form below is acceptable per the P01
# plan (Phase 3.5 RESEARCH Pattern 1 skeleton uses <RESOLVE_AT_BUILD_TIME>).
FROM python:3.12-slim-bookworm AS final

ARG NAIW_VERSION
ARG NAIW_GIT_SHA

# HOME=/tmp/naiw-home — when wrapper passes `--user UID:GID`, /etc/passwd has
# no entry for the operator UID; setting HOME explicitly makes Python's
# expanduser('~') and git's ~/.gitconfig lookup work without a passwd entry.
# /tmp is tmpfs-backed at runtime per D-H1. DOCKER_API_VERSION pins the docker
# CLI to API 1.43, matching naiw_tasks.docker_client.PINNED_DOCKER_API_VERSION.
# PYTHONDONTWRITEBYTECODE=1 silences pyc-write failures under read_only:true
# rootfs (compileall below installs them once at build time).
ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    HOME=/tmp/naiw-home \
    DOCKER_API_VERSION=1.43 \
    PYTHONDONTWRITEBYTECODE=1

# git for naiw_tasks.git_ops subprocess calls; docker-ce-cli for
# os.execvpe('docker', ..., 'attach', ...) from naiw_tasks/attach.py
# (CLAUDE.md 'What NOT to Use' rejects docker-py's pseudo-TTY attach —
# issues #247/#390/#983). docker-ce-cli installs from Docker's official
# Debian apt repo (signed by /etc/apt/keyrings/docker.asc).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# Install wheels from builder stage (no source files in final image), then
# pre-compile site-packages bytecode so first-invocation latency under
# read_only:true rootfs stays flat (Pitfall 7 from 03.5-RESEARCH.md).
COPY --from=builder /wheels/*.whl /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels \
    && python -m compileall -q -j 0 /usr/local/lib/python3.12/site-packages/naiw_tasks /usr/local/lib/python3.12/site-packages/naiw_common

# Fallback identity if `docker compose run --user UID:GID` is bypassed. The
# wrapper (scripts/install-wrapper.sh from P03) always passes --user so files
# in /naiw-data land with the operator's UID. UID 1000 matches the common
# host operator UID, same as the Phase 1 task image's `pi` user.
RUN useradd -m -u 1000 -s /bin/bash naiw

USER 1000
WORKDIR /naiw-data

LABEL naiw.managed="1" \
      naiw.role="controller" \
      naiw.version="${NAIW_VERSION}" \
      naiw.git-sha="${NAIW_GIT_SHA}" \
      naiw.docker-api-version="1.43" \
      naiw.python-version="3.12" \
      org.opencontainers.image.source="https://github.com/d954mas/neotolis-ai-workbench"

# naiw-tasks console script is registered by the naiw_tasks wheel
# (pyproject.toml [project.scripts]). Exec-form propagates SIGTERM/SIGINT
# from `docker compose run --rm`.
ENTRYPOINT ["naiw-tasks"]
