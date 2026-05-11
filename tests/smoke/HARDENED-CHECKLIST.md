# NAIW Hardened-Lifecycle Smoke Checklist

This file is the **contract** for the hardened lifecycle gate.
`tests/smoke/run-hardened-smoke.sh` is the **executor**: it runs each verification
command listed below and emits `[hardened-smoke] [<ID>] ok|FAIL: ...` on stdout/stderr.

**Audit semantics:**
- Every row in this table MUST correspond to a `[hardened-smoke] [<ID>]` printout in the bash gate.
- Every `[hardened-smoke] [<ID>]` printout in the bash gate MUST correspond to a row in this table.
- Drift between the checklist and the script (orphan ID on either side) is itself a smoke failure,
  enforced by the script's `diff <(grep -oE 'HARD-[0-9]+|PROXY-[0-9]+' ...)` drift gate.

**Gate exit contract:**
- All rows must be `ok` (or `PARTIAL` for rows explicitly marked so below) for the gate to exit 0.
- `PARTIAL` rows count as passing for the gate's exit-0 semantics — they document half-coverage
  where the OTHER half is owned by a different phase. Phase 6 (ops finalisation) updates these to
  full PASS once the cross-phase work lands.

**How to run:**

```bash
bash tests/smoke/run-hardened-smoke.sh    # operator-driven on Linux host
pytest tests/smoke/test_hardened.py -v    # via pytest wrapper (same gate; pytest assertion frame)
```

On a non-Linux host (Windows-native, `/mnt/c/`-backed `$HOME`), the gate prints
`[hardened-smoke] SKIP: requires Linux host with Linux-FS` and exits 0.

## Requirements Coverage

| ID       | Requirement summary                                                                      | Verification (gate step) | Status note |
|----------|------------------------------------------------------------------------------------------|--------------------------|-------------|
| HARD-01  | `--cap-drop=ALL` applied (or smallest verified allow set; never less restrictive)        | step 03                  |             |
| HARD-02  | `--security-opt=no-new-privileges` applied                                               | step 04                  |             |
| HARD-03  | `--read-only` rootfs + tmpfs `/tmp` (512m), `/run` (64m), `/home/pi` (128m) writable     | steps 05, 06, 07, 08     |             |
| HARD-04  | `--pids-limit=512` baseline                                                              | step 09                  |             |
| HARD-05  | `--memory=4g --memory-swap=4g --cpus=2` limits applied                                   | step 10                  |             |
| HARD-06  | `--network naiw-task-net` (private bridge; no host networking, no docker socket)         | step 11                  |             |
| HARD-07  | `tasks/<id>/meta/` is NEVER bind-mounted into the container                              | step 12                  |             |
| HARD-08  | only writable bind-mounts are `/work` and `/io`; only read-only bind-mounts are `/pi-packages` and `/run/secrets/<name>` | step 13 |             |
| HARD-09  | bind-mount source paths validated by controller via `Path.resolve()` + prefix check (DEFENSE is in Phase 3; THREAT BASELINE here) | step 14 | **PARTIAL** — threat baseline @ Phase 2.5 (this script, source-symlink probe via alpine sidecar); defense @ Phase 3 (controller `Path.resolve()` + prefix check). Counts as PASS for gate exit-0. |
| HARD-10  | `--restart=no` (recovery is operator-driven)                                             | step 15                  |             |
| PROXY-05 | All non-allowlisted operations return 403 from naiw-docker-proxy                         | steps 20-41              | Covers 20 denied verb probes + 2 allowed probes (GET `/containers/json`, POST `/containers/<id>/start`). Denied verb grid: EXEC, IMAGES, VOLUMES, NETWORKS, BUILD, INFO, AUTH, SECRETS, SERVICES, SESSION, SWARM, SYSTEM, TASKS, PLUGINS, NODES, CONFIGS, DISTRIBUTION, EVENTS, PING, VERSION. |

## Lifecycle Cross-Cutting Validations

Not requirement-bound, but required by the gate for end-to-end confidence:

| Validation               | Description                                                                                                                            | Gate step |
|--------------------------|----------------------------------------------------------------------------------------------------------------------------------------|-----------|
| PID-1 wrapper            | `/proc/1/comm` in {`tmux`, `tini`, `docker-init`} under `docker run --init`                                                            | step 16   |
| Secret content + scrub   | `/run/secrets/test_token` readable; not in `docker inspect Config.Env`; raw token NOT in `terminal.log`; `[REDACTED]` IS in `terminal.log` | step 17 |
| Stop+start log survival  | `terminal.log` size before stop <= size after start AND post-restart marker appears                                                    | step 18   |
| Signal cycle             | `naiw-signal done --summary "hardened-smoke ok"` appends a valid JSON line to `events.jsonl` with `kind=done`, `schema_version=1`, expected payload | step 19 |
| cgroup peak evidence     | `pids.peak` and `memory.peak` logged; WARN-only on 60%-of-limit breach; `n/a` on cgroup v1 hosts                                       | step 42   |

## Failure Mode

On gate FAIL, the cleanup trap is **asymmetric**:

- **Success path:** container removed (`docker rm -f`), tmpdir removed (`rm -rf "$TMP"`),
  compose brought down (`docker compose down`). `naiw-task-net` is PRESERVED (long-lived).
  Final stdout line: `[hardened-smoke] PASS — all 11 requirements verified`.

- **Fail path:** container PRESERVED (not removed), tmpdir PRESERVED, compose STAYS UP.
  `naiw-task-net` PRESERVED. Final stderr lines list the preserved artifacts for inspection.
  Operator-driven manual cleanup after fail:

  ```bash
  docker rm -f naiw-smoke-hardened-<pid>
  docker rm -f naiw-smoke-probe-target-<pid>      # if created
  rm -rf $HOME/.naiw-smoke/smoke.<XXXXXXXX>
  docker compose -f deploy/docker-compose.yml down
  ```

## Phase Boundaries

- **Phase 2.5 (this file):** Verifies runtime contract of the hardened lifecycle on Phase-1 artifacts (image, compose, entrypoint, naiw-signal). No controller code is written in this phase.
- **Phase 3 (out of scope here):** Controller code that constructs `docker run` arguments and enforces the HARD-09 source-path defense via `Path.resolve()` + prefix check against `~/naiw-data/`. After Phase 3 ships, HARD-09 graduates from `PARTIAL` to full PASS.
- **Phase 6 (out of scope here):** OPS-01 drift audit reads this checklist and verifies running containers' `HostConfig` still matches.
