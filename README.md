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

- **Distribution:** host-installed CLI. Run `bash scripts/install-controller.sh` — it provisions both `naiw_tasks` AND its in-repo `naiw_common` wire-format dep into the same `uv tool` / `pipx` venv (with a venv fallback if neither is present). A bare `pipx install -e ./src/naiw_tasks` will fail because pipx isolates per-tool venvs and `naiw_common` is not published to any index — by design; the script enforces the correct install path. Runs as the operator's user, not as root.
- **Endpoint:** `tcp://127.0.0.1:2375` (localhost-bound proxy port). Override via `docker_proxy_url` in `~/naiw-data/config.yaml` if needed.
- **`docker attach`:** the CLI invokes `docker -H tcp://127.0.0.1:2375 attach <name>` so the docker CLI also goes through the proxy (without `-H`, it would silently fall back to `/var/run/docker.sock`).
- **SDK:** `docker` Python SDK 7.1 with `base_url=tcp://127.0.0.1:2375`.
- **Allowed Engine API surface (effective):**
  - `GET /containers/*` (list, inspect, logs)
  - `POST /containers/create` — creates ANY container with operator-supplied image, mounts, and host-config kwargs. Controller pins this to `naiw-task-image` + the hardened HostConfig kwargs, but the proxy itself does not restrict the image or mounts. A direct HTTP caller can pick anything.
  - `POST /containers/<id>/start`, `/stop`, `/attach`, restart/kill paths
  - `POST /containers/<id>/exec` (the exec CREATE endpoint) — **not blocked**: it lives under `/containers/*` and goes through `CONTAINERS=1+POST=1`. Creates an exec instance but does NOT start it.
- **Denied:** `POST /exec/<id>/start`, `/exec/<id>/resize`, `GET /exec/<id>/json` (so the created exec instance cannot actually run), `images/*`, `volumes/*`, `networks/*`, `build/*`, every Swarm endpoint. See `deploy/proxy/README.md` for the full deny list.

### Trust boundary (read this before deploying)

The proxy is bound to `127.0.0.1:2375`. On Linux TCP localhost is **not user-scoped** — any process running as **any user on this host** can connect to it. In practice that means:

| Caller | Access |
|---|---|
| Remote host (LAN/Internet) | ❌ blocked (binding is 127.0.0.1, not 0.0.0.0) |
| Another local user on this host | ✅ **can connect** (TCP localhost ignores uid) |
| The operator's own processes (`npm install` post-install scripts, browser extensions with native messaging, `pip install` packages, IDE plugins, etc.) | ✅ **can connect** |

What such a caller can do via the allowlist above:

- Create a container with `Image: alpine`, `HostConfig.Binds: ["/:/host"]`, `HostConfig.Privileged: true` and start it → root-on-host via a side-loaded container, **bypassing every NAIW hardening setting** because those settings only constrain containers the controller itself creates.
- List, inspect, and stop any container — including the operator's other NAIW tasks.

**This is the same threat model as having the operator's user in the `docker` group** with a local Docker daemon: trusted code runs as the operator with broad container-control authority. NAIW is single-user, single-host (per `CLAUDE.md`) and assumes the operator audits the code they install. If that assumption does not hold for your deployment, you need a stricter proxy (e.g., `wollomatic/socket-proxy` with per-name/per-image/per-mount regex allowlist) — see "Alternatives Considered" in `CLAUDE.md`. The current proxy is **not** "attach/start/stop only"; it is "containers/* + POST + start/stop", which is substantial Docker control surface.

The previous design (controller container joining `naiw-internal`, no host port published) avoided this exposure but cost operator UX. The current design accepts the same-user-trust assumption to keep `naiw-tasks <cmd>` a simple host CLI invocation. The container-level isolation (`cap_drop=ALL`, `read_only`, separate `naiw-task-net`, `--privileged: false`, …) is still enforced for every container the controller creates — that boundary protects Pi inside a task. The trust boundary discussed here is at a different layer: same-host code outside the controller.

## Project rules

- `CLAUDE.md` — tech-stack lock, hardening reference, "What NOT to Use" list.
- `AGENTS.md` — KISS/DRY principles.
- `task.md` — original MVP brief.
