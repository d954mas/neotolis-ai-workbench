# naiw-docker-proxy

Tecnativa Docker socket proxy. Single point of Docker Engine access for the NAIW controller.

## Why a proxy?

Mounting `/var/run/docker.sock` directly into any container gives that container root-equivalent access on the host. The controller (Phase 3+) NEVER mounts the host socket. Instead, controller and proxy share a private bridge network (`naiw-internal`) and the controller talks HTTP to `tcp://naiw-docker-proxy:2375`. The proxy gates every Engine API path; the locked allowlist below is the canonical security boundary.

See `CLAUDE.md` "What NOT to Use" — direct socket mount is a permanent reject.

## Locked allowlist (D-26 / PROXY-02)

Five env vars are set to `"1"`. Everything else (every documented Tecnativa env var) is set to `"0"` (paranoid-explicit). The compose file is its own audit log.

| Env var | Gates | Why allowed |
|---|---|---|
| `CONTAINERS=1` | `GET /containers/*` (list, inspect, logs) | Controller `naiw-tasks list`, `naiw-tasks output` (host-side reads, but inspect is for status reconciliation) |
| `POST=1` | ANY `POST`/`PUT`/`DELETE` (global gate) | Required for `start`/`stop`. Without this, even allow-listed actions fail. |
| `ALLOW_START=1` | `POST /containers/<id>/start` | Controller `naiw-tasks start` |
| `ALLOW_STOP=1` | `POST /containers/<id>/stop` | Controller `naiw-tasks finish` |
| `ALLOW_RESTARTS=1` | stop/restart/kill paths | Controller `naiw-tasks finish` (graceful stop fallthrough) |

## Locked deny (24 env vars set to "0")

Flipping any of these to `"1"` is a security regression and MUST go through a documented PROJECT-level decision change:

`EXEC` (D3 — permanent close), `IMAGES`, `VOLUMES`, `NETWORKS`, `BUILD`, `INFO`, `EVENTS`, `PING`, `VERSION`, `AUTH`, `SECRETS` (Swarm), `SERVICES`, `SESSION`, `SWARM`, `SYSTEM`, `TASKS` (Swarm), `PLUGINS`, `NODES`, `CONFIGS`, `DISTRIBUTION`, `COMMIT`, `GRPC`, `ALLOW_PAUSE`, `ALLOW_UNPAUSE`.

## Network model

- Proxy is on `naiw-internal` (private bridge).
- **No `ports:` mapping** — proxy never publishes a host port (D-23 / PROXY-03).
- Controller (Phase 3) joins `naiw-internal` and reaches the proxy at `http://naiw-docker-proxy:2375`.
- Task containers (Phase 3) are on a SEPARATE network `naiw-task-net` and do NOT reach the proxy. Task containers have no Docker access.

## Healthcheck

No compose healthcheck is configured. The locked allowlist sets `PING=0` (paranoid-explicit), which makes the proxy correctly return 403 to the `/_ping` path — empirically verified by Plan 06's proxy smoke harness. There is no allowed HTTP endpoint that a healthcheck could probe without weakening the security boundary, so we rely on `restart: unless-stopped` and the operator's `docker compose ps` for liveness.

If a future phase needs a probe (e.g., a downstream service uses `depends_on: { condition: service_healthy }`), use a TCP-level check that does not traverse the allowlist:

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

Manual; updating the digest is a code change visible in `git diff`. CONTEXT-Deferred: `scripts/update-proxy-digest.sh` may automate this in v2.

## Empirical allowlist verification

Phase 1 locks the *configuration*. Phase 2.5 runs the *empirical grid*:
- Sibling container probes `/v1.43/containers/json` → expects 200.
- Sibling probes `/v1.43/containers/<fakeid>/exec` → expects 403.
- Sibling iterates IMAGES/VOLUMES/NETWORKS/BUILD endpoints → expects 403 each.

Phase 1 smoke (Plan 05) does the minimum positive+negative pair (one 200, one 403) for early-warning detection of misconfigurations.

## Bring up / tear down

```bash
docker compose -f deploy/docker-compose.yml up -d   # start proxy
docker compose -f deploy/docker-compose.yml ps      # check status
docker compose -f deploy/docker-compose.yml down    # remove
```
