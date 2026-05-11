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
- All rows must be `ok` for the gate to exit 0.

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
| HARD-01  | `--cap-drop=ALL` applied (or smallest verified allow set; never less restrictive)        | step 04                  |             |
| HARD-02  | `--security-opt=no-new-privileges` applied                                               | step 05                  |             |
| HARD-03  | `--read-only` rootfs + tmpfs `/tmp` (512m), `/run` (64m), `/home/pi` (128m) writable     | steps 03, 06, 07, 08     |             |
| HARD-04  | `--pids-limit=512` baseline                                                              | step 09                  |             |
| HARD-05  | `--memory=4g --memory-swap=4g --cpus=2` limits applied                                   | step 10                  |             |
| HARD-06  | `--network naiw-task-net` (private bridge; no host networking, no docker socket)         | step 11                  |             |
| HARD-07  | `tasks/<id>/meta/` is NEVER bind-mounted into the container                              | step 12                  |             |
| HARD-08  | only writable bind-mounts are `/work` and `/io`; only read-only bind-mounts are `/pi-packages` and `/run/secrets/<name>` | step 13 |             |
| HARD-10  | `--restart=no` (recovery is operator-driven)                                             | step 14                  |             |
| PROXY-05 | All non-allowlisted operations return 403 from naiw-docker-proxy                         | steps 19-40              | Covers 20 denied verb probes + 2 allowed probes (GET `/containers/json`, POST `/containers/<id>/start`). Denied verb grid: EXEC, IMAGES, VOLUMES, NETWORKS, BUILD, INFO, AUTH, SECRETS, SERVICES, SESSION, SWARM, SYSTEM, TASKS, PLUGINS, NODES, CONFIGS, DISTRIBUTION, EVENTS, PING, VERSION. |

## Lifecycle Cross-Cutting Validations

Not requirement-bound, but required by the gate for end-to-end confidence:

| Validation               | Description                                                                                                                            | Gate step |
|--------------------------|----------------------------------------------------------------------------------------------------------------------------------------|-----------|
| PID-1 wrapper            | `/proc/1/comm` in {`tmux`, `tini`, `docker-init`} under `docker run --init`                                                            | step 15   |
| Secret content + scrub   | `/run/secrets/test_token` readable; not in `docker inspect Config.Env`; raw token NOT in `terminal.log`; `[REDACTED]` IS in `terminal.log` | step 16 |
| Stop+start log survival  | `terminal.log` size before stop <= size after start AND post-restart marker appears                                                    | step 17   |
| Signal cycle             | `naiw-signal done --summary "hardened-smoke ok"` appends a valid JSON line to `events.jsonl` with `kind=done`, `schema_version=1`, expected payload | step 18 |
| cgroup peak evidence     | `pids.peak` and `memory.peak` logged; WARN-only on 60%-of-limit breach; `n/a` on cgroup v1 hosts                                       | step 41   |

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

## Out of Scope

HARD-09 (controller-side bind-mount source-path validation via `Path.resolve()` + prefix check
against `~/naiw-data/`) is not verified here. The defense lives in the controller, which
is built in a later phase; its smoke probe will be added then.
