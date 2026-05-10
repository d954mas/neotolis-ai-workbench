# Neotolis AI Workbench (NAIW)

Self-hosted, single-user task and session manager. Each task runs in its own disposable, sandboxed Docker container with its own folder, tmux session, and (for project tasks) git worktree. See `task.md` and `CLAUDE.md` for the full charter.

## Phase 1 — Foundation (current)

Phase 1 produces five static artifacts. No controller logic yet.

| Artifact | Path |
|---|---|
| `naiw-task-image` Dockerfile + entrypoint | `image/` |
| `naiw-docker-proxy` compose file | `deploy/docker-compose.yml` |
| Host data-layout bootstrap scripts | `scripts/naiw-init-data.sh`, `scripts/naiw-new-task.sh` |
| `naiw_common` + `naiw_signal` Python packages | `src/` |
| Image build helper | `scripts/build-image.sh` |

## Architecture (Phase 1 scope)

- **Image** — `naiw-task-image` bakes Pi (`@earendil-works/pi-coding-agent`) + tmux + git + gh + node + python + ffmpeg + ripgrep + the `naiw-signal` wheel. Runs as non-root `pi` user. Default `CMD ["tmux","new-session","-A","-s","main"]` — Phase 3 controller attaches via `docker attach` (NOT `exec`).
- **Proxy** — `tecnativa/docker-socket-proxy` pinned by sha256 on a private `naiw-internal` Docker bridge. **No host port published.** Controller (Phase 3) joins `naiw-internal` and talks to `tcp://naiw-docker-proxy:2375`. Locked allowlist: see `deploy/proxy/README.md`.
- **Host data layout** — `~/naiw-data/` contains `projects.yaml`, `secrets/` (mode 0700), `pi-packages/`, `workspace/repos/`, and per-task `tasks/<id>/{meta,work,io}` directories. `meta/` is host-only (never mounted into the container).
- **Signal CLI** — `naiw-signal done|fail|wait` runs inside the image as the `pi` user, appends one JSON event line to `/io/.naiw/events.jsonl`. Pure file-writer; no Docker access; never reads `meta/`.

## Quick start (operator, after `git clone`)

```bash
# 1. Bootstrap host data layout
bash scripts/naiw-init-data.sh

# 2. Build the task image (resolves PI_VERSION and git SHA automatically)
bash scripts/build-image.sh

# 3. Bring up the Docker socket proxy
docker compose -f deploy/docker-compose.yml up -d

# 4. Verify (Plan 05 smoke harnesses)
bash tests/smoke/run-image-smoke.sh
bash tests/smoke/run-proxy-smoke.sh
```

## Controller contract (Phase 3, NOT implemented in Phase 1)

The controller communicates with the Docker engine via the proxy ONLY. No direct socket mount is permitted.

- **Endpoint:** `tcp://naiw-docker-proxy:2375`
- **Network:** controller container joins `naiw-internal`
- **SDK:** `docker` Python SDK 7.1 with `DOCKER_HOST=tcp://naiw-docker-proxy:2375`
- **Allowed operations:** `containers/*` (list, inspect, logs), `containers/<id>/start`, `containers/<id>/stop`, restart/kill paths.
- **Denied:** `exec/*`, `images/*`, `volumes/*`, `networks/*`, `build/*`, every Swarm endpoint. See `deploy/proxy/README.md` for the full deny list.

## Roadmap

See `.planning/ROADMAP.md` for the six-phase plan plus the Phase 2.5 hardened-smoke gate.

## Project rules

- `CLAUDE.md` — tech-stack lock, hardening reference, "What NOT to Use" list.
- `AGENTS.md` — KISS/DRY principles.
- `task.md` — original MVP brief.
