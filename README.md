# Neotolis AI Workbench (NAIW)

Self-hosted, single-user task and session manager. Each task runs in its own disposable, sandboxed Docker container with its own folder, tmux session, and (for project tasks) git worktree. See `task.md` and `CLAUDE.md` for the full charter.

## Current scope

The Phase 3 controller (`naiw-tasks` CLI) is implemented and ships in this repo. Operator workflow: start a per-task hardened container, attach a terminal, finish with a worktree-cleanup policy.

| Artifact | Path |
|---|---|
| `naiw-task-image` Dockerfile + entrypoint | `image/` |
| `naiw-docker-proxy` compose file | `deploy/docker-compose.yml` |
| Host data-layout bootstrap scripts | `scripts/naiw-init-data.sh`, `scripts/naiw-new-task.sh` |
| `naiw_common` + `naiw_signal` Python packages | `src/naiw_common/`, `src/naiw_signal/` |
| `naiw_tasks` controller (`naiw-tasks` CLI) | `src/naiw_tasks/` |
| Image build helper | `scripts/build-image.sh` |
| Controller wrapper installer (Phase 3.5) | `scripts/install-wrapper.sh` |
| Controller Dockerfile (Phase 3.5) | `image/controller.Dockerfile` |
| CI build+push workflow (Phase 3.5) | `.github/workflows/build-images.yml` |

## Architecture

- **Image** — `naiw-task-image` bakes Pi (`@earendil-works/pi-coding-agent`) + tmux + git + gh + node + python + ffmpeg + ripgrep + the `naiw-signal` wheel. Runs as non-root `pi` user. Default `CMD ["tmux","new-session","-A","-s","main"]` — the controller attaches via `docker attach` (NOT `exec`).
- **Proxy** — `tecnativa/docker-socket-proxy` pinned by sha256 on a private `naiw-internal` Docker bridge. **Publishes no host port** (Phase 3.5 D-N2). The containerized controller (`naiw-controller` service on `naiw-internal`) reaches it via internal DNS at `tcp://naiw-docker-proxy:2375`. Locked allowlist enforces the security boundary; see `deploy/proxy/README.md`.
- **Task network** — Task containers run on a separate `naiw-task-net` bridge created by the same compose file. Tasks have NO route to the proxy or to `/var/run/docker.sock`.
- **Host data layout** — `~/naiw-data/` contains `projects.yaml`, `secrets/` (mode 0700), `pi-packages/`, `workspace/repos/`, and per-task `tasks/<id>/{meta,work,io}` directories. `meta/` is host-only (never mounted into the container).
- **Signal CLI** — `naiw-signal done|fail|wait` runs inside the image as the `pi` user, appends one JSON event line to `/io/.naiw/events.jsonl`. Pure file-writer; no Docker access; never reads `meta/`.

## Quick start (operator, after `git clone`)

```bash
# 1. Bootstrap host data layout
bash scripts/naiw-init-data.sh

# 2. Install the system compose file (requires sudo for /etc/naiw)
sudo install -d /etc/naiw
sudo cp deploy/docker-compose.yml /etc/naiw/docker-compose.yml

# 3. Pull both images (controller + task image) from ghcr
docker compose -f /etc/naiw/docker-compose.yml pull

# 4. Start the proxy (controller is one-shot via `docker compose run`)
docker compose -f /etc/naiw/docker-compose.yml up -d naiw-docker-proxy

# 5. Install the operator wrapper to ~/.local/bin/naiw-tasks
bash scripts/install-wrapper.sh   # also pre-pulls ghcr.io/d954mas/naiw-task-image:latest

# 6. Verify
naiw-tasks --help                                  # delegates to docker compose run
bash tests/smoke/run-containerized-smoke.sh        # full smoke (Linux only)
```

The host needs Docker + the docker-compose plugin; **Python is not required** on the host. Operator edits `~/naiw-data/projects.yaml`, drops files in `~/naiw-data/secrets/`, and clones repos under `~/naiw-data/workspace/repos/` using normal host tools (D-S2).

If you previously ran the Phase-3 host-CLI installer (now removed per D-M3), uninstall the pipx package once: `pipx uninstall naiw_tasks` (or `uv tool uninstall naiw_tasks`). No automated migration is provided because Phase 3 was not deployed in production (CONTEXT.md D-M1 overrides ROADMAP SC #5).

## Controller contract

The controller (`naiw-tasks` CLI) communicates with the Docker engine via the proxy ONLY. No direct socket mount is permitted.

- **Distribution:** Docker image `ghcr.io/d954mas/naiw-controller:<tag>` (published by `.github/workflows/build-images.yml`). `deploy/docker-compose.yml` currently uses the `:latest` tag so `docker compose pull` works on a fresh install; once the first CI publish lands, the operator pins the controller line to `ghcr.io/d954mas/naiw-controller@sha256:<DIGEST>` for immutability. The task image (`ghcr.io/d954mas/naiw-task-image:latest`) is the runtime source of truth (`config.DEFAULT_TASK_IMAGE`) and is also mutable until pinned via `~/naiw-data/config.yaml`. Wrapper at `~/.local/bin/naiw-tasks` delegates every call to `docker compose run --rm --user "$(id -u):$(id -g)" naiw-controller "$@"` (`-it` when a TTY is attached, `-T` for cron/pipe/systemd). One-shot run mode — every `naiw-tasks` call spawns a fresh container.
- **Endpoint:** `tcp://naiw-docker-proxy:2375` (internal DNS on `naiw-internal`; D-N3 default). Operator override via `docker_proxy_url` in `~/naiw-data/config.yaml`.
- **`docker attach`:** the controller invokes `docker -H tcp://naiw-docker-proxy:2375 attach <name>` from inside the controller container so the CLI also goes through the proxy.
- **SDK:** `docker` Python SDK 7.1 with `base_url=tcp://naiw-docker-proxy:2375`; API version pinned to 1.43.

Allowed engine surface, denied surface: see `deploy/proxy/README.md` for the full table.

## Trust boundary (Phase 3.5)

Phase 3.5 isolates the controller in its own hardened container on a private Docker network; the proxy publishes no host port.

| Component         | Network         | Allowed access                          | Threat model                                                       |
|-------------------|-----------------|------------------------------------------|---------------------------------------------------------------------|
| `naiw-controller` | `naiw-internal` | proxy via DNS (`tcp://naiw-docker-proxy:2375`) | trusted code, holds Docker control surface via the locked proxy allowlist |
| `naiw-docker-proxy` | `naiw-internal` | `/var/run/docker.sock:ro` (host) | locked allowlist (`CONTAINERS=1`, `POST=1`, `ALLOW_START/STOP/RESTARTS=1`, everything else `0` paranoid-explicit) |
| task containers (Pi) | `naiw-task-net` | no proxy, no daemon, no host socket | untrusted Pi runs here; container hardening (cap-drop=ALL, read-only, no-new-privileges, …) is the boundary |

### What changed from Phase 3

Previously the proxy published a localhost port so a host-installed Python CLI could reach it; that exposed substantial Docker control surface to any same-host process. Phase 3.5 puts the controller back inside `naiw-internal` and unpublishes the proxy port. The Phase-3 round-4 localhost-binding caveat is no longer applicable and has been removed.

### What did NOT change

Container hardening (`cap-drop=ALL`, `read-only`, separate `naiw-task-net`, no `--privileged`, …) for task containers is unchanged. The proxy allowlist (5 yes-vars, 24 paranoid-zeros) is unchanged. The trust assumption — operator audits the code they run on this host — is unchanged.

## Project rules

- `CLAUDE.md` — tech-stack lock, hardening reference, "What NOT to Use" list.
- `AGENTS.md` — KISS/DRY principles.
- `task.md` — original MVP brief.
