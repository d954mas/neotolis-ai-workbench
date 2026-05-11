# naiw-docker-proxy

Tecnativa Docker socket proxy. Single point of Docker Engine access for the NAIW controller.

## Why a proxy?

Mounting `/var/run/docker.sock` directly into any container or process gives that consumer root-equivalent access on the host. The controller never mounts the host socket. Instead, the proxy lives on a private bridge network (`naiw-internal`), reads the host socket itself (mounted read-only), and exposes a heavily-locked HTTP API. The proxy gates every Engine API path; the locked allowlist below is the canonical security boundary.

See `CLAUDE.md` "What NOT to Use" — direct socket mount is a permanent reject.

## Locked allowlist

Five env vars are set to `"1"`. Everything else (every documented Tecnativa env var) is set to `"0"` (paranoid-explicit). The compose file is its own audit log.

| Env var | Gates | Why allowed |
|---|---|---|
| `CONTAINERS=1` | `GET /containers/*` (list, inspect, logs) | `list` and `output` (host-side reads); `inspect` for status reconciliation |
| `POST=1` | ANY `POST`/`PUT`/`DELETE` (global gate) | Required for `start`/`stop`. Without this, even allow-listed actions fail. |
| `ALLOW_START=1` | `POST /containers/<id>/start` | Task `start` |
| `ALLOW_STOP=1` | `POST /containers/<id>/stop` | Task `finish` |
| `ALLOW_RESTARTS=1` | stop/restart/kill paths | Task `finish` (graceful stop fallthrough) |

## Locked deny (24 env vars set to "0")

Flipping any of these to `"1"` is a security regression and MUST go through a documented project-level decision change:

`EXEC` (permanent close), `IMAGES`, `VOLUMES`, `NETWORKS`, `BUILD`, `INFO`, `EVENTS`, `PING`, `VERSION`, `AUTH`, `SECRETS` (Swarm), `SERVICES`, `SESSION`, `SWARM`, `SYSTEM`, `TASKS` (Swarm), `PLUGINS`, `NODES`, `CONFIGS`, `DISTRIBUTION`, `COMMIT`, `GRPC`, `ALLOW_PAUSE`, `ALLOW_UNPAUSE`.

## Network model

- Proxy is on `naiw-internal` (private bridge).
- **`ports: ["127.0.0.1:2375:2375"]`** — published to LOCALHOST ONLY. `0.0.0.0:2375` here is a security regression and MUST be rejected in code review.
- The host-installed controller (`naiw-tasks` CLI via pipx / uv tool) reaches the proxy at `tcp://127.0.0.1:2375`. The proxy's locked allowlist is the security boundary; the localhost-only binding is defense-in-depth (no LAN process can hit even allow-listed endpoints).
- Task containers run on a SEPARATE network `naiw-task-net` (created by the same compose file) and do NOT reach the proxy. Task containers have no Docker access.
- Why this is not the same threat as "exposing the Docker socket": the bare socket exposes a full unrestricted Engine API. A locked proxy in front of the socket only exposes the five allow-listed endpoint groups (`CONTAINERS`, `POST`, `ALLOW_START`, `ALLOW_STOP`, `ALLOW_RESTARTS`). The threat model collapses to: an attacker with local-host code execution could already mount the host socket directly, so localhost-only proxy access is not a downgrade.

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
