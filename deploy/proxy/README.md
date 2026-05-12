# naiw-docker-proxy

Tecnativa Docker socket proxy. Single point of Docker Engine access for the NAIW controller.

## Why a proxy?

Mounting `/var/run/docker.sock` directly into any container or process gives that consumer root-equivalent access on the host. The controller never mounts the host socket. Instead, the proxy lives on a private bridge network (`naiw-internal`), reads the host socket itself (mounted read-only), and exposes a heavily-locked HTTP API. The proxy gates every Engine API path; the locked allowlist below is the canonical security boundary.

See `CLAUDE.md` "What NOT to Use" — direct socket mount is a permanent reject.

## Locked allowlist

Five env vars are set to `"1"`. Everything else (every documented Tecnativa env var) is set to `"0"` (paranoid-explicit). The compose file is its own audit log.

| Env var | Gates | Why allowed |
|---|---|---|
| `CONTAINERS=1` | `GET` AND (via `POST=1`) any write method on `/containers/*` (list, inspect, logs, create, **delete**, exec-create) | `list` and `output` (host-side reads); `inspect` for status reconciliation; `create`/`delete` for task lifecycle |
| `POST=1` | ANY `POST`/`PUT`/`DELETE` on every `*=1` endpoint group above (global gate) | Required for `start`/`stop`/`remove`. Combined with `CONTAINERS=1` it permits `DELETE /containers/<id>?force=true` (used by `container.remove(force=True)` in `finish`). Without this, even allow-listed actions fail. |
| `ALLOW_START=1` | `POST /containers/<id>/start` | Task `start` |
| `ALLOW_STOP=1` | `POST /containers/<id>/stop` | Task `finish` |
| `ALLOW_RESTARTS=1` | stop/restart/kill paths | Task `finish` (graceful stop fallthrough) |

## Locked deny (24 env vars set to "0")

Flipping any of these to `"1"` is a security regression and MUST go through a documented project-level decision change:

`EXEC` (permanent close), `IMAGES`, `VOLUMES`, `NETWORKS`, `BUILD`, `INFO`, `EVENTS`, `PING`, `VERSION`, `AUTH`, `SECRETS` (Swarm), `SERVICES`, `SESSION`, `SWARM`, `SYSTEM`, `TASKS` (Swarm), `PLUGINS`, `NODES`, `CONFIGS`, `DISTRIBUTION`, `COMMIT`, `GRPC`, `ALLOW_PAUSE`, `ALLOW_UNPAUSE`.

## Network model

- Proxy is on `naiw-internal` (private bridge).
- **`ports: ["127.0.0.1:2375:2375"]`** — published to LOCALHOST ONLY. `0.0.0.0:2375` here is a security regression and MUST be rejected in code review.
- The host-installed controller (`naiw-tasks` CLI via pipx / uv tool) reaches the proxy at `tcp://127.0.0.1:2375`.
- Task containers run on a SEPARATE network `naiw-task-net` (created by the same compose file) and do NOT reach the proxy. Task containers have no Docker access.

## Trust boundary (operator must read)

TCP localhost on Linux is **not user-scoped** — any process running as **any user on this host** can connect to `127.0.0.1:2375`. Combined with the allowlist below, the effective trust boundary is "same-host processes", NOT "controller only".

What a same-host caller can do via this proxy:

- `POST /containers/create` with arbitrary `Image`, `HostConfig.Binds`, `HostConfig.Privileged: true` — the proxy enforces the endpoint allowlist but NOT the contents of the create request. A side-loaded `--privileged` container with `/:/host` mount is a complete bypass of NAIW's container isolation (which only applies to containers the controller itself builds).
- `DELETE /containers/<id>?force=true` — `POST=1` is the global write-method gate (POST + PUT + DELETE). Combined with `CONTAINERS=1`, force-remove on any container is allowed. The controller uses this for `container.remove(force=True)` in `finish`; a same-host caller can use it to nuke arbitrary containers, including the operator's other NAIW tasks.
- `POST /containers/<id>/exec` (CREATE) is under `/containers/*` and **allowed** — an exec instance can be created. Only `/exec/<id>/start` (and resize/json) is blocked by `EXEC=0`, so the instance never actually runs; this still does NOT make the exec surface "fully closed" as a simplified mental model would suggest.

This is the same trust assumption as having the operator's user in the `docker` group: NAIW assumes the operator audits the code they run on this host (npm packages, IDE plugins, browser extensions with native messaging, etc.). The current proxy is **not** "attach/start/stop only" — it is "containers/* + POST + start/stop", which is substantial Docker control surface.

If a deployment cannot make this assumption (multi-user host, untrusted local code), the proxy needs per-name/per-image/per-mount regex allowlisting (e.g., `wollomatic/socket-proxy` instead of `tecnativa/docker-socket-proxy`), or the controller has to move back into `naiw-internal` with no host port published — see "Alternatives Considered" in the project `CLAUDE.md`.

## Locked allowlist — operational summary

`CONTAINERS=1`, `POST=1`, `ALLOW_START=1`, `ALLOW_STOP=1`, `ALLOW_RESTARTS=1`. Everything else (`EXEC`, `IMAGES`, `VOLUMES`, `NETWORKS`, `BUILD`, `INFO`, `EVENTS`, `PING`, `VERSION`, `AUTH`, Swarm-family) is `0` paranoid-explicit. Adding any `=1` is a security regression and MUST go through a documented project-level decision change.

> Note on `EXEC=0`: this gates `/exec/*` paths only — i.e., `POST /exec/<id>/start`, `/exec/<id>/resize`, `GET /exec/<id>/json`. It does **not** gate `POST /containers/<id>/exec` (the create endpoint), which lives under `/containers/*` and goes through `CONTAINERS+POST`. Effective behavior: a caller can create exec instances but cannot start them, so no in-container code execution actually runs — but the API surface is wider than "exec is closed".

## Healthcheck

No compose healthcheck is configured. The locked allowlist sets `PING=0` (paranoid-explicit), which makes the proxy correctly return 403 to the `/_ping` path — empirically verified by the proxy smoke harness. There is no allowed HTTP endpoint that a healthcheck could probe without weakening the security boundary, so we rely on `restart: unless-stopped` and the operator's `docker compose ps` for liveness.

If a future caller needs a probe (e.g., a downstream service uses `depends_on: { condition: service_healthy }`), use a TCP-level check that does not traverse the allowlist:

    healthcheck:
      test: ["CMD-SHELL", "exec 3<>/dev/tcp/localhost/2375 || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3

This opens a TCP connection to port 2375 and exits 0 if haproxy accepts it. It does NOT issue any HTTP request and therefore does NOT touch the allowlist.

## Digest update procedure

The image is pinned by sha256 in `docker-compose.yml`. To update:

```bash
docker pull tecnativa/docker-socket-proxy:v0.4.2
docker inspect --format='{{index .RepoDigests 0}}' tecnativa/docker-socket-proxy:v0.4.2
# → tecnativa/docker-socket-proxy@sha256:<NEW_DIGEST>
# Edit deploy/docker-compose.yml; replace the previous digest with the new one.
# Test locally: docker compose -f deploy/docker-compose.yml up -d && docker compose ps
```

Manual; updating the digest is a code change visible in `git diff`.

## Empirical allowlist verification

The smoke harness in `tests/smoke/run-proxy-smoke.sh` issues the minimum positive+negative pair (one 200, one 403) for early-warning detection of misconfigurations. A full per-endpoint grid (IMAGES, VOLUMES, NETWORKS, BUILD, …) belongs in the hardened-lifecycle smoke suite.

## Bring up / tear down

```bash
docker compose -f deploy/docker-compose.yml up -d   # start proxy
docker compose -f deploy/docker-compose.yml ps      # check status
docker compose -f deploy/docker-compose.yml down    # remove
```
