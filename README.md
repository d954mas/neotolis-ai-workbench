# Neotolis AI Workbench (NAIW)

Self-hosted, single-user task and session manager. Each task runs in its own disposable, sandboxed Docker container with its own folder, tmux session, and (for project tasks) git worktree. See `task.md` and `CLAUDE.md` for the full charter.

## Current scope

Five static artifacts that the controller will build on top of. The controller itself is not implemented yet.

| Artifact | Path |
|---|---|
| `naiw-task-image` Dockerfile + entrypoint | `image/` |
| `naiw-docker-proxy` compose file | `deploy/docker-compose.yml` |
| Host data-layout bootstrap scripts | `scripts/naiw-init-data.sh`, `scripts/naiw-new-task.sh` |
| `naiw_common` + `naiw_signal` Python packages | `src/` |
| Image build helper | `scripts/build-image.sh` |

## Architecture

- **Image** — `naiw-task-image` bakes Pi (`@earendil-works/pi-coding-agent`) + tmux + git + gh + node + python + ffmpeg + ripgrep + the `naiw-signal` wheel. Runs as non-root `pi` user. Default `CMD ["tmux","new-session","-A","-s","main"]` — the controller attaches via `docker attach` (NOT `exec`).
- **Proxy** — `tecnativa/docker-socket-proxy` pinned by sha256 on a private `naiw-internal` Docker bridge. **Publishes 2375 on `127.0.0.1` only** (localhost-bound; not LAN-exposed). The host-installed `naiw-tasks` CLI reaches the proxy at `tcp://127.0.0.1:2375`. Locked allowlist enforces the security boundary regardless of the binding; see `deploy/proxy/README.md`.
- **Task network** — Task containers run on a separate `naiw-task-net` bridge created by the same compose file. Tasks have NO route to the proxy or to `/var/run/docker.sock`.
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

# 4. Verify
bash tests/smoke/run-image-smoke.sh
bash tests/smoke/run-proxy-smoke.sh
```

## Controller contract

The controller (`naiw-tasks` CLI) communicates with the Docker engine via the proxy ONLY. No direct socket mount is permitted.

- **Distribution:** host-installed CLI (`pipx install -e ./src/naiw_tasks` or `uv tool install ...`). Runs as the operator's user, not as root.
- **Endpoint:** `tcp://127.0.0.1:2375` (localhost-bound proxy port). Override via `docker_proxy_url` in `~/naiw-data/config.yaml` if needed.
- **Why localhost binding is safe:** the proxy itself enforces the locked allowlist; localhost-only `ports:` mapping (not `0.0.0.0`) is defense-in-depth so no LAN process can hit even the allow-listed endpoints. The original concern with exposing port 2375 was about the raw Docker socket — that's a different threat model. A locked proxy bound to 127.0.0.1 is the standard pattern for host-based Docker tooling.
- **`docker attach`:** the CLI invokes `docker -H tcp://127.0.0.1:2375 attach <name>` so the docker CLI also goes through the proxy (without `-H`, it would silently fall back to `/var/run/docker.sock`).
- **SDK:** `docker` Python SDK 7.1 with `base_url=tcp://127.0.0.1:2375`.
- **Allowed operations:** `containers/*` (list, inspect, logs), `containers/<id>/start`, `containers/<id>/stop`, `containers/<id>/attach`, restart/kill paths.
- **Denied:** `exec/*`, `images/*`, `volumes/*`, `networks/*`, `build/*`, every Swarm endpoint. See `deploy/proxy/README.md` for the full deny list.

## Project rules

- `CLAUDE.md` — tech-stack lock, hardening reference, "What NOT to Use" list.
- `AGENTS.md` — KISS/DRY principles.
- `task.md` — original MVP brief.
