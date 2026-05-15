# syntax=docker/dockerfile:1

ARG NAIW_VERSION=0.1.0
ARG NAIW_GIT_SHA=unknown
ARG DOCKER_CE_CLI_VERSION=5:27.3.1-1~debian.12~bookworm

FROM python:3.12-slim-bookworm AS builder
WORKDIR /build
RUN pip install --no-cache-dir build==1.2.*
COPY src/naiw_common/ ./naiw_common/
COPY src/naiw_tasks/ ./naiw_tasks/
RUN python -m build --wheel --outdir /wheels ./naiw_common \
    && python -m build --wheel --outdir /wheels ./naiw_tasks

FROM python:3.12-slim-bookworm AS final

ARG NAIW_VERSION
ARG NAIW_GIT_SHA
ARG DOCKER_CE_CLI_VERSION

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    HOME=/tmp/naiw-home \
    DOCKER_API_VERSION=1.43 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli="${DOCKER_CE_CLI_VERSION}" \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels/*.whl /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels \
    && python -m compileall -q -j 0 \
        /usr/local/lib/python3.12/site-packages/naiw_tasks \
        /usr/local/lib/python3.12/site-packages/naiw_common

RUN useradd -u 1000 naiw

USER 1000
WORKDIR /naiw-data

LABEL naiw.managed="1" \
      naiw.role="controller" \
      naiw.version="${NAIW_VERSION}" \
      naiw.git-sha="${NAIW_GIT_SHA}" \
      naiw.docker-api-version="1.43" \
      naiw.docker-ce-cli-version="${DOCKER_CE_CLI_VERSION}" \
      naiw.python-version="3.12" \
      org.opencontainers.image.source="https://github.com/d954mas/neotolis-ai-workbench"

ENTRYPOINT ["naiw-tasks"]
