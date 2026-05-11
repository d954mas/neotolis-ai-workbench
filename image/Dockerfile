# syntax=docker/dockerfile:1
# naiw-task-image — multi-stage build.
# Stage 1 builds wheels for naiw_common and naiw_signal; final image installs
# them and bakes everything Pi needs (tmux, git, gh, node, python, ffmpeg, ripgrep).

ARG PI_VERSION
ARG NAIW_VERSION=0.1.0
ARG NAIW_GIT_SHA=unknown

# ── Stage 1: builder — produce wheels for naiw_common and naiw_signal ──
FROM python:3.12-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir build==1.2.*
COPY src/naiw_common/ ./naiw_common/
COPY src/naiw_signal/ ./naiw_signal/
RUN python -m build --wheel --outdir /wheels ./naiw_common && \
    python -m build --wheel --outdir /wheels ./naiw_signal

# ── Stage 2: runtime — debian:12-slim, pinned by sha256 (refresh procedure below) ──
# To refresh:
#   docker pull debian:12-slim
#   docker inspect --format='{{index .RepoDigests 0}}' debian:12-slim
FROM debian@sha256:67b30a61dc87758f0caf819646104f29ecbda97d920aaf5edc834128ac8493d3 AS final

ARG PI_VERSION
ARG NAIW_VERSION
ARG NAIW_GIT_SHA

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PI_SKIP_VERSION_CHECK=1

# System packages: tmux, git, python3, ffmpeg, ripgrep, build-essential.
# Node 20.x via NodeSource (Pi requires >= 20.6.0).
# gh via official GitHub apt repo (signed by github keyring).
#
# apt packages are intentionally NOT version-pinned. The base image is
# digest-pinned (which controls the apt source state), and floating versions
# let Debian security patches flow through automatically on rebuild. Revisit
# if bit-for-bit reproducibility becomes a requirement — in that case use
# snapshot.debian.org + explicit `pkg=version` pins.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
        tmux git \
        python3 python3-pip \
        ffmpeg ripgrep \
        build-essential \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -f /usr/lib/python3.*/EXTERNALLY-MANAGED \
    && rm -rf /var/lib/apt/lists/*

# PEP 668: Debian 12's python3 ships an EXTERNALLY-MANAGED marker that makes
# `pip install --user` refuse to run. Pi-tasks install their dependencies via
# pip --user at runtime — that's part of the Pi-runtime contract — so the
# marker is removed above. Safe inside a container: the env is isolated, there
# is no APT-managed Python to protect from breakage.

# Pi (pi-coding-agent) — version pinned via PI_VERSION build arg.
RUN npm install -g --no-fund --no-audit "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && command -v pi >/dev/null || { echo 'pi not on PATH' >&2; exit 1; }

# Wheels from builder stage (no source files in the final image).
COPY --from=builder /wheels/*.whl /tmp/wheels/
RUN pip3 install --no-cache-dir /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels

# Bake config: redaction sed for pipe-pane, tmux config, system gitconfig with
# the credential helper that resolves /run/secrets/github_token, and the entrypoint.
COPY image/etc/naiw/redact.sed /etc/naiw/redact.sed
COPY image/etc/tmux.conf /etc/tmux.conf
COPY image/etc/gitconfig /etc/gitconfig
COPY image/etc/naiw-git-credential-helper /usr/local/bin/naiw-git-credential-helper
COPY image/entrypoint.sh /usr/local/bin/naiw-entrypoint
RUN chmod 0755 /usr/local/bin/naiw-entrypoint /usr/local/bin/naiw-git-credential-helper

# Non-root `pi` user. UID 1000 to match common host operator UID.
# Empty ~/.gitconfig (system gitconfig is the only credential source).
# No ~/.ssh/ is created — secrets arrive via /run/secrets file mounts only.
RUN useradd -m -u 1000 -s /bin/bash pi \
    && touch /home/pi/.gitconfig \
    && chown pi:pi /home/pi/.gitconfig

# Belt-and-braces: NO docker CLI in image. Pi must never reach Docker.
# Build fails if any prior layer accidentally pulled one in.
RUN ! command -v docker

USER pi
WORKDIR /work

LABEL naiw.managed="1" \
      naiw.role="task-image" \
      naiw.version="${NAIW_VERSION}" \
      naiw.git-sha="${NAIW_GIT_SHA}" \
      naiw.pi-version="${PI_VERSION}" \
      naiw.signal-schema="1" \
      org.opencontainers.image.source="https://github.com/d954mas/neotolis-ai-workbench"

# tmux is the CMD; entrypoint hands off via `exec tmux attach`.
# This keeps tmux as PID 1 (or PID 2 under `docker run --init`) so `docker attach`
# is sufficient — no `docker exec` is needed for the operator session.
ENTRYPOINT ["/usr/local/bin/naiw-entrypoint"]
CMD ["tmux", "new-session", "-A", "-s", "main"]
