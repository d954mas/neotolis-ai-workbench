# syntax=docker/dockerfile:1
# image/Dockerfile — naiw-task-image (Phase 1, multi-stage; D-03)

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

# ── Stage 2: runtime — naiw-task-image (debian:12-slim digest-pinned per D-09) ──
# Resolved at plan execution via:
#   docker pull debian:12-slim
#   docker inspect --format='{{index .RepoDigests 0}}' debian:12-slim
# → debian@sha256:67b30a61dc87758f0caf819646104f29ecbda97d920aaf5edc834128ac8493d3
FROM debian@sha256:67b30a61dc87758f0caf819646104f29ecbda97d920aaf5edc834128ac8493d3 AS final

ARG PI_VERSION
ARG NAIW_VERSION
ARG NAIW_GIT_SHA

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PI_SKIP_VERSION_CHECK=1

# System packages (IMG-02): tmux, git, python3, ffmpeg, ripgrep, build-essential.
# Node 20.x via NodeSource (Pi requires >= 20.6.0 per RESEARCH).
# gh via official GitHub apt repo (RESEARCH §Pattern 1).
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
    && rm -rf /var/lib/apt/lists/*

# Pi (pi-coding-agent) — IMG-01. Pinned via PI_VERSION build arg (D-10).
RUN npm install -g --no-fund --no-audit "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && command -v pi >/dev/null || { echo 'pi not on PATH' >&2; exit 1; }

# Wheels from builder stage (D-03; no source files in final image).
COPY --from=builder /wheels/*.whl /tmp/wheels/
RUN pip3 install --no-cache-dir --break-system-packages /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels

# Bake config files (D-14, D-15, D-17).
COPY image/etc/naiw/redact.sed /etc/naiw/redact.sed
COPY image/etc/tmux.conf /etc/tmux.conf
COPY image/etc/gitconfig /etc/gitconfig
COPY image/etc/naiw-git-credential-helper /usr/local/bin/naiw-git-credential-helper
COPY image/entrypoint.sh /usr/local/bin/naiw-entrypoint
RUN chmod 0755 /usr/local/bin/naiw-entrypoint /usr/local/bin/naiw-git-credential-helper

# Non-root pi user (IMG-09). UID 1000 to match common host operator UID.
# Empty ~/.gitconfig per GIT-02 / D-16. No ~/.ssh/ created (D-15 file-mount only).
RUN useradd -m -u 1000 -s /bin/bash pi \
    && touch /home/pi/.gitconfig \
    && chown pi:pi /home/pi/.gitconfig

# Belt-and-braces SIG-04: NO docker CLI in image. Build fails if any prior layer pulled one in.
RUN ! command -v docker

USER pi
WORKDIR /work

# IMG-10 / D-30: canonical label set.
LABEL naiw.managed="1" \
      naiw.role="task-image" \
      naiw.version="${NAIW_VERSION}" \
      naiw.git-sha="${NAIW_GIT_SHA}" \
      naiw.pi-version="${PI_VERSION}" \
      naiw.signal-schema="1" \
      org.opencontainers.image.source="https://github.com/d954mas/neotolis-ai-workbench"

# IMG-04 / D3 attach contract: tmux is the CMD; entrypoint hands off via `exec tmux attach`.
ENTRYPOINT ["/usr/local/bin/naiw-entrypoint"]
CMD ["tmux", "new-session", "-A", "-s", "main"]
