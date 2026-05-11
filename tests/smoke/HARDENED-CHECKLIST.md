# NAIW Hardened-Lifecycle Smoke Checklist

Contract for `tests/smoke/run-hardened-smoke.sh`. Every row below must
correspond to a `step_check "<ID>"` line in the script; the script's
drift gate (step 42) enforces this. Run:

```bash
bash tests/smoke/run-hardened-smoke.sh    # Linux host
pytest tests/smoke/test_hardened.py -v    # same gate via pytest
```

On non-Linux hosts the gate prints `[hardened-smoke] SKIP:` and exits 0.
On FAIL the container, tmpdir, and compose stack are preserved for
inspection; on PASS they are torn down. `naiw-task-net` is always kept.

## Requirements

| ID       | Requirement summary                                                                  | Gate step           |
|----------|--------------------------------------------------------------------------------------|---------------------|
| HARD-01  | `--cap-drop=ALL`                                                                     | 04                  |
| HARD-02  | `--security-opt=no-new-privileges`                                                   | 05                  |
| HARD-03  | `--read-only` rootfs + tmpfs `/tmp` (512m), `/run` (64m), `/home/pi` (128m) writable | 03, 06, 07, 08      |
| HARD-04  | `--pids-limit=512`                                                                   | 09                  |
| HARD-05  | `--memory=4g --memory-swap=4g --cpus=2`                                              | 10                  |
| HARD-06  | `--network naiw-task-net` (no host net, no docker socket)                            | 11                  |
| HARD-07  | `tasks/<id>/meta/` never bind-mounted                                                | 12                  |
| HARD-08  | writable bind-mounts only `/work` and `/io`; read-only `/pi-packages`, `/run/secrets/<name>` | 13          |
| HARD-10  | `--restart=no`                                                                       | 14                  |
| PROXY-05 | All non-allowlisted ops return 403 (20 denied verbs + 2 allowed)                     | 19-40               |

HARD-09 (controller-side bind-mount source-path validation) lives in the
controller and is verified when that phase ships.

## Lifecycle

| Validation              | Gate step |
|-------------------------|-----------|
| PID-1 wrapper (tmux/tini/docker-init) | 15 |
| Secret readable + Config.Env scrub + terminal.log redaction | 16 |
| `terminal.log` survives stop+start; post-restart marker appears | 17 |
| `naiw-signal done` appends valid event to `events.jsonl` | 18 |
| cgroup `pids.peak` + `memory.peak` logged (WARN-only) | 41 |
